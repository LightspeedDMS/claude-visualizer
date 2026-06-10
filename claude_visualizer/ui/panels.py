"""Panel widgets and pure renderers for the 3-region TUI shell.

Rendering is split so the *formatting* logic is pure and unit-testable without
spinning up Textual:

- :func:`format_mru_row` turns one :class:`MruEntry` into a single display line
  of ``file_path  project · <short session>`` plus a ``⤷sub`` marker when the
  source is a subagent transcript.
- :func:`render_mru` turns a whole :class:`MruModel` into a newest-first text
  block (or a placeholder when empty), highlighting the row whose path equals
  ``model.highlighted_path`` (the file whose diff is currently displayed — the
  F3 ↔ F4 sync of AC9).
- :func:`format_diff_header` / :func:`render_diff_body` / :func:`render_diff`
  turn an immutable :class:`DisplayState` (from the pure ``DiffQueueModel``)
  into a Rich renderable for the top-right Diff panel: a
  ``model · 🧠 · filename · origin`` header over a colour-mapped diff body
  (``COLOR_FOR_KIND``) with a ``+N more`` badge and any truncation footer.

- :func:`truncate_command` / :func:`format_command_row` / :func:`render_commands`
  turn a :class:`~claude_visualizer.models.command_feed.CommandFeedModel` into a
  newest-on-top, width-truncated, origin-tagged text block for the bottom
  Commands feed panel (story #4).

The :class:`MruFilesPanel` / :class:`DiffPanel` / :class:`CommandsPanel` widgets
are thin Textual ``Static``s that, on ``update_from_*``, call the matching pure
renderer and push the result into the widget.  No IO and no parsing happen here
— the panels only read already-populated models, satisfying "no IO/parse on the
render path".
"""

from __future__ import annotations

import math
import os
from datetime import datetime
from typing import List, Optional

from rich.text import Text
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Static

from claude_visualizer.diffing import COLOR_FOR_KIND, DiffSegment
from claude_visualizer.models.command_feed import CommandFeedEntry, CommandFeedModel
from claude_visualizer.models.diff_queue import DisplayState
from claude_visualizer.models.mru import MruEntry, MruModel

# Marker appended to a row when its source transcript is a subagent
# (``.../subagents/agent-*.jsonl``).
SUBAGENT_MARKER = "⤷sub"

# Separator between the project tag and the short session id in the origin tag.
ORIGIN_SEP = "·"

# Shown in the MRU panel before any file activity has been observed.
MRU_EMPTY_TEXT = "Waiting for file activity across sessions…"

# Header line for the active panel.
MRU_TITLE = "Most Recent File Events"

# Prefix marking the highlighted (currently-displayed) row in the MRU list so
# the selection is visible both in the rendered text (tests) and on screen.
MRU_HIGHLIGHT_MARKER = "▶ "
# Indent kept on non-highlighted rows so columns stay aligned under the marker.
_MRU_ROW_INDENT = "  "
# Rich style applied to the highlighted MRU row (visible in the screenshot).
MRU_HIGHLIGHT_STYLE = "bold reverse"
# Subtle background tint applied to every ODD MRU row (global position, stable
# across scrolling) so adjacent entries are visually separated even when a long
# path wraps onto multiple terminal lines.  Even rows inherit the terminal
# default and carry no extra span.
MRU_ROW_STYLE_ODD = "on #262626"
# Full style for odd rows: bright foreground on dark background so text is
# readable against the tinted background (diff-editor contrast style).
MRU_ROW_STYLE_ODD_FULL = f"bright_white {MRU_ROW_STYLE_ODD}"
# Foreground colour for even rows: slightly muted so even/odd alternate visually
# without requiring a background tint on even rows.
MRU_ROW_STYLE_EVEN = "#aaaaaa"

# --- Diff panel (story #3) ------------------------------------------------
# Header line for the Diff panel.
DIFF_TITLE = "Live Diff — most-recent file"
# Shown in the Diff panel before any file modification has been observed.
DIFF_EMPTY_TEXT = "Waiting for a file modification…"
# The 🧠 marker is shown in the header iff the edit's request used extended
# thinking (AC3/AC4).
THINKING_GLYPH = "🧠"
# Separator between the header fields (model · 🧠 · filename · origin).
HEADER_SEP = " · "
# Leading text identifying the model label as a short, ``claude-``-stripped form.
_MODEL_PREFIX = "claude-"
# Placeholder shown when an event carries no model label.
_UNKNOWN_MODEL = "model?"
# Rows reserved above/around the diff body when sizing the scroll window: the
# panel border, the title line, the header line, and a blank spacer.  The app
# subtracts this from the panel's measured height to get the body viewport.
DIFF_CHROME_ROWS = 4
# Fallback body height used before the panel has a measured size, so the very
# first tick still computes a sensible scroll window (AC6).
DIFF_DEFAULT_VIEWPORT = 20

# Rows reserved for the panel title chrome (title line + blank spacer) when
# computing the page-scroll step size.  Subtracting this from the panel's
# content height gives the approximate number of data rows visible at once.
_PANEL_TITLE_CHROME = 2

# Large delta used for Home/End key scrolling — intentionally larger than any
# realistic list so that clamping in _scroll_mru/_scroll_commands always lands
# at the true start (0) or end (len-1) regardless of current content length.
_SCROLL_TO_EXTREME = 999_999

# --- Commands feed (story #4) ---------------------------------------------
# Header line for the bottom Commands feed panel.
COMMANDS_TITLE = "Commands — live cross-session Bash & MCP feed"
# Shown in the Commands panel before any Bash command has been observed.
COMMANDS_EMPTY_TEXT = "Waiting for Bash commands across sessions…"
# Ellipsis appended when a command is truncated to fit the panel width (AC3).
TRUNCATION_ELLIPSIS = "…"
# Stable placeholder shown in the time column when an event carries no
# timestamp, so a row never collapses or raises (fail-soft on display only —
# the command/origin are still shown).
MISSING_TIME_TEXT = "--:--:--"
# Time-of-day format for a command row's timestamp column.
TIME_FORMAT = "%H:%M:%S"


def _wrapped_line_count(plain_text: str, width: int) -> int:
    """Return the number of physical terminal lines occupied by ``plain_text`` at ``width``.

    Uses integer ceiling division so a 1-char row at width=1 returns 1, not 0.
    Always returns at least 1 so empty rows still occupy one physical line.

    Pure — no Textual, no side effects.  Used by both :meth:`MruFilesPanel.on_mouse_down`
    (click→row mapping) and :func:`viewport_offset_for_cursor` (cursor-follow).
    """
    if width <= 0:
        return 1
    return max(1, (len(plain_text) + width - 1) // width)


def viewport_offset_for_cursor(
    selected: int,
    scroll_offset: int,
    visible_rows: int,
    total: int,
    *,
    line_counts: Optional[list],
) -> int:
    """Wrap-aware viewport offset that keeps ``selected`` visible in the viewport.

    Like :func:`clamp_viewport_to_cursor` but accounts for MRU rows that wrap to
    multiple physical terminal lines: ``line_counts[i]`` is the number of physical
    lines row *i* occupies at the current panel width.  When a preceding row wraps
    to 3 lines it occupies 3 slots in the visible window, leaving less room for
    rows that follow.

    Algorithm:
    1. Walk rows from ``scroll_offset`` forward, accumulating physical lines.
    2. If ``selected`` is reached before the accumulator exceeds ``visible_rows``
       → cursor is already visible → return ``scroll_offset`` unchanged.
    3. If we exhaust the window before reaching ``selected`` → cursor is below →
       advance ``scroll_offset`` until the cursor fits.
    4. If ``selected < scroll_offset`` → cursor is above → snap ``scroll_offset``
       to ``selected``.

    Fallback: if ``line_counts`` is ``None`` or empty every row is treated as 1
    physical line, making this equivalent to :func:`clamp_viewport_to_cursor`.
    Result is always clamped to ``[0, max(0, total - 1)]``.

    Parameters
    ----------
    selected:
        Logical row index of the cursor (0-based).
    scroll_offset:
        Current first-visible logical row index.
    visible_rows:
        Physical terminal lines available for the list body (after chrome).
    total:
        Total number of logical rows in the model.
    line_counts:
        Per-row physical line count list.  ``None`` or ``[]`` → 1 per row.
    """
    if not line_counts:
        # Unmounted fallback: treat every row as 1 physical line.
        return clamp_viewport_to_cursor(selected, scroll_offset, visible_rows, total)

    visible_rows = max(1, visible_rows)

    # Cursor above the viewport → snap offset to cursor row.
    if selected < scroll_offset:
        return max(0, selected)

    # Check whether cursor is already visible within the current window.
    accumulated = 0
    for row_idx in range(scroll_offset, total):
        wc = line_counts[row_idx] if row_idx < len(line_counts) else 1
        if row_idx == selected:
            # The cursor row starts at ``accumulated`` physical lines from the
            # top of the viewport.  It is visible if that starting point is
            # strictly less than visible_rows (there is at least one line for it).
            if accumulated < visible_rows:
                return scroll_offset  # cursor is visible — no change
            break  # cursor starts beyond the window — need to scroll
        accumulated += wc
        if accumulated >= visible_rows:
            # Window is full before we even reached the cursor.
            break

    # Cursor is not visible — advance scroll_offset one row at a time until it is.
    new_offset = scroll_offset
    while new_offset < selected:
        new_offset += 1
        # Check if cursor is now visible from new_offset.
        accumulated = 0
        for row_idx in range(new_offset, total):
            wc = line_counts[row_idx] if row_idx < len(line_counts) else 1
            if row_idx == selected:
                if accumulated < visible_rows:
                    return max(0, min(new_offset, max(0, total - 1)))
                break
            accumulated += wc
            if accumulated >= visible_rows:
                break

    return max(0, min(new_offset, max(0, total - 1)))


def clamp_viewport_to_cursor(
    selected: int,
    scroll_offset: int,
    visible_rows: int,
    total: int,
) -> int:
    """Return the new scroll_offset that keeps ``selected`` visible in the viewport.

    Pure arithmetic helper — no Textual, no side effects.  Used by
    :class:`MruFilesPanel` to implement standard listbox cursor-follow semantics:

    - If ``selected`` is already within ``[scroll_offset, scroll_offset + visible_rows - 1]``
      the offset is returned unchanged.
    - If ``selected`` is above the window: ``offset = selected``.
    - If ``selected`` is below the window: ``offset = selected - visible_rows + 1``.
    - Result is always clamped to ``[0, max(0, total - visible_rows)]`` so the
      viewport never shows a gap below the last row.

    Parameters match the widget's state fields so callers can test the pure
    arithmetic without mounting a Textual panel (where ``content_size.height`` is
    always 0 pre-layout).
    """
    visible_rows = max(1, visible_rows)
    if selected < scroll_offset:
        new_offset = selected
    elif selected >= scroll_offset + visible_rows:
        new_offset = selected - visible_rows + 1
    else:
        new_offset = scroll_offset
    # Clamp to valid range.
    max_offset = max(0, total - visible_rows)
    return max(0, min(new_offset, max_offset))


def diff_viewport_height(
    height: int,
    *,
    chrome_rows: int = DIFF_CHROME_ROWS,
    default: int = DIFF_DEFAULT_VIEWPORT,
) -> int:
    """Rows available for the diff *body* given the panel's measured ``height``.

    Pure arithmetic shared by the app (which passes the live panel height) and
    the unit tests, so the scroll-window sizing lives in exactly one place:

    - ``height <= 0`` (pre-layout / unmeasured) → fall back to ``default`` so the
      first tick still produces a reasonable window.
    - otherwise subtract the ``chrome_rows`` (border + title + header + spacer)
      and floor at 1 so even a panel smaller than the chrome yields one body row.
    """
    if height <= 0:
        return default
    return max(1, height - chrome_rows)


def format_mru_row(entry: MruEntry, highlighted: bool = False) -> str:
    """Render one MRU entry as a single display line.

    Layout: ``<time>  <prefix>[OP] <file_path>   <project> · <short_session>``
    with a trailing ``⤷sub`` marker when ``entry.is_subagent`` is true.  The
    timestamp is the LEFTMOST column — it appears before the ``[OP]`` marker and
    the file path.  The op is shown as a compact ``[WRITE]``/``[EDIT]`` tag so
    the row conveys what happened.  The ``HH:MM:SS`` time (or
    :data:`MISSING_TIME_TEXT` when the event carries no timestamp) is formatted
    by the SHARED :func:`_format_time` helper (DRY — the same formatter the feed
    and diff header use) and never raises on ``None``.

    When ``highlighted`` (the file whose diff is currently on screen — AC9) the
    ``prefix`` is :data:`MRU_HIGHLIGHT_MARKER`; otherwise it is the same-width
    indent :data:`_MRU_ROW_INDENT` so every row's columns stay aligned.
    """
    time_str = _format_time(entry.ts)
    origin = f"{entry.project_tag} {ORIGIN_SEP} {entry.short_session}"
    op_tag = f"[{entry.op.value}]"
    prefix = MRU_HIGHLIGHT_MARKER if highlighted else _MRU_ROW_INDENT
    line = f"{time_str}  {prefix}{op_tag} {entry.file_path}   {origin}"
    if entry.is_subagent:
        line = f"{line}  {SUBAGENT_MARKER}"
    return line


def _styled_mru_row(entry: MruEntry, prefix: str) -> Text:
    """Build an MRU row as a Rich Text with per-field color accents.

    Used for NON-highlighted rows only.  Highlighted rows continue to use the
    plain :func:`format_mru_row` string with :data:`MRU_HIGHLIGHT_STYLE`
    because ``bold reverse`` conflicts with per-field foreground colors.
    """
    time_str = _format_time(entry.ts)
    op_tag = f"[{entry.op.value}]"
    row = Text()
    row.append(time_str, style="dim")
    row.append("  ")
    row.append(prefix)
    row.append(op_tag, style="dim cyan")
    row.append(" ")
    row.append(entry.file_path)
    row.append("   ")
    row.append(entry.project_tag, style="dim green")
    row.append(f" {ORIGIN_SEP} ", style="dim")
    row.append(entry.short_session, style="dim")
    if entry.is_subagent:
        row.append("  ")
        row.append(SUBAGENT_MARKER, style="dim yellow")
    return row


def render_mru(
    model: MruModel,
    scroll_offset: int = 0,
    width: int = 0,
    *,
    focused: bool = False,
) -> Text:
    """Render the MRU model as a newest-first Rich block, newest at the top.

    Returns a waiting message (still inside the titled block) when the model
    holds no rows, so the panel never shows a blank void before activity
    begins.  The row whose path equals ``model.highlighted_path`` — the file
    whose diff is currently displayed in the top-right panel — is rendered with
    :data:`MRU_HIGHLIGHT_STYLE` so the F3 ↔ F4 sync (AC9) is visible on screen.

    ``scroll_offset`` skips the first N rows so the panel can be scrolled
    through a list longer than the visible area.  A ``scroll_offset`` of 0
    (the default) shows from the top, so all existing call-sites are unchanged.

    ``width`` when > 0 pads each row with trailing spaces so the background
    colour fills the full panel width (diff-editor style), rather than ending
    at the last text character.

    ``focused`` when True renders the title with ``bold reverse`` so the panel
    title highlights to indicate keyboard focus (no border change, no layout
    shift).
    """
    out = Text()
    title_style = "bold reverse" if focused else "bold"
    out.append(f"{MRU_TITLE}\n\n", style=title_style)
    rows = model.rows()
    if not rows:
        out.append(MRU_EMPTY_TEXT)
        return out
    visible = rows[scroll_offset:]
    if not visible:
        out.append(MRU_EMPTY_TEXT)
        return out
    for index, entry in enumerate(visible):
        is_hl = entry.event_key == model.highlighted_key
        if is_hl:
            # Highlighted rows: plain string + MRU_HIGHLIGHT_STYLE.
            # bold reverse conflicts with per-field colors; keep plain.
            row_text = format_mru_row(entry, highlighted=True)
            if width > 0:
                line_count = max(1, math.ceil(len(row_text) / width))
                target = line_count * width
                padded = row_text + " " * (target - len(row_text))
                for chunk_start in range(0, target, width):
                    out.append(
                        padded[chunk_start : chunk_start + width],
                        style=MRU_HIGHLIGHT_STYLE,
                    )
                    if chunk_start + width < target:
                        out.append("\n")
            else:
                out.append(row_text, style=MRU_HIGHLIGHT_STYLE)
        else:
            # Non-highlighted rows: Rich Text with per-field color accents.
            styled_row = _styled_mru_row(entry, _MRU_ROW_INDENT)
            is_odd = (scroll_offset + index) % 2 == 1
            if width > 0:
                plain_len = len(styled_row)
                line_count = max(1, math.ceil(plain_len / width))
                target = line_count * width
                if plain_len < target:
                    styled_row.append(" " * (target - plain_len))
                # Apply ONLY background for zebra — don't override field colors.
                if is_odd:
                    styled_row.stylize("on #262626")
                # Chunk into width-sized pieces (Text slicing preserves styles).
                for chunk_start in range(0, target, width):
                    out.append_text(styled_row[chunk_start : chunk_start + width])
                    if chunk_start + width < target:
                        out.append("\n")
            else:
                if is_odd:
                    styled_row.stylize("on #262626")
                out.append_text(styled_row)
        if index < len(visible) - 1:
            out.append("\n")
    return out


class MruFilesPanel(Static):
    """Active top-left panel: a live, newest-first list of modified files.

    The panel is a pure view over an :class:`MruModel`.  ``update_from_model``
    re-renders from the model's current snapshot; it performs no IO and never
    parses transcripts — the pipeline owns all of that and only feeds the model.
    The rendered value is a Rich :class:`~rich.text.Text` (so the highlighted
    row is styled on screen); ``rendered_text`` exposes its plain string for
    substring assertions in tests.

    ``can_focus = True`` so Tab/Shift+Tab cycle through the three panels.

    Smart cursor: ``_selected_index`` is the logical cursor/highlight position
    (0-based into ``self._rows``).  ``_scroll_offset`` is the viewport top.
    Navigation keys (↑/↓/Page/Home/End) move ``_selected_index`` and then call
    :func:`clamp_viewport_to_cursor` to adjust ``_scroll_offset`` only when the
    cursor leaves the visible window — standard listbox behaviour.  Mouse wheel
    pans the viewport (``_scroll_offset``) directly WITHOUT moving the cursor or
    posting :class:`FileClicked`.
    """

    can_focus = True
    ALLOW_SELECT = False

    class FileClicked(Message):
        """Posted when the user clicks a file row in the MRU list.

        ``event_key`` is the ``(file_path, ts)`` tuple identifying the clicked
        event — the same key used by the diff queue's ``pin()`` method.
        """

        def __init__(self, event_key: tuple) -> None:
            super().__init__()
            self.event_key = event_key

    def __init__(self, **kwargs) -> None:
        super().__init__(MRU_EMPTY_TEXT, **kwargs)
        self._renderable: Text = Text(MRU_EMPTY_TEXT)
        self._rows: list = []  # snapshot of MruEntry rows for click mapping
        self._scroll_offset: int = 0
        self._selected_index: int = 0  # logical cursor — moves on key nav
        self._last_model: Optional[MruModel] = (
            None  # stored for scroll re-render without a new model push
        )
        self._last_width: int = 0  # stored for scroll re-render at same width

    def _visible_rows(self) -> int:
        """Number of data rows visible at once (content height minus title chrome).

        Pre-layout (``content_size.height == 0``) → returns 1 as a safe minimum.
        This is the SAME measure used by ``_page_step``; ``_page_step`` delegates
        here so there is a single source of truth.
        """
        return max(1, self.content_size.height - _PANEL_TITLE_CHROME)

    def update_from_model(self, model: MruModel, width: int = 0) -> None:
        """Re-render the panel from the model's current rows."""
        self._last_model = model
        self._last_width = width
        self._rows = model.rows()  # snapshot for click-to-row mapping
        # Clamp both cursor and viewport so a shrinking list leaves them valid.
        if self._rows:
            self._selected_index = min(self._selected_index, len(self._rows) - 1)
            self._scroll_offset = min(self._scroll_offset, len(self._rows) - 1)
        else:
            self._selected_index = 0
            self._scroll_offset = 0
        self._renderable = render_mru(
            model, self._scroll_offset, width, focused=self.has_focus
        )
        if self.is_mounted:
            self.refresh()

    def _scroll_mru(self, delta: int) -> None:
        """Pan the viewport by ``delta`` rows (wheel / direct callers only).

        This is a VIEWPORT-only operation — it does NOT move ``_selected_index``
        and does NOT post :class:`FileClicked`.  Mouse wheel handlers call this
        directly.  Keyboard navigation calls ``_navigate_cursor`` instead.
        """
        if not self._rows or self._last_model is None:
            return
        self._scroll_offset = max(
            0, min(self._scroll_offset + delta, len(self._rows) - 1)
        )
        self._renderable = render_mru(
            self._last_model,
            self._scroll_offset,
            self._last_width,
            focused=self.has_focus,
        )
        if self.is_mounted:
            self.refresh()

    def _navigate_cursor(self, delta: int) -> None:
        """Move ``_selected_index`` by ``delta``, clamp viewport, repaint, select.

        This is the KEYBOARD navigation primitive:
        1. Clamp ``_selected_index + delta`` to ``[0, len(rows)-1]``.
        2. Call :func:`viewport_offset_for_cursor` (wrap-aware) to adjust
           ``_scroll_offset`` only when the cursor leaves the visible window.
           When mounted (``content_size.width > 0``) the per-row physical line
           counts are computed from the plain-text row lengths so wrapped entries
           count as multiple physical lines.  When unmounted (pre-layout,
           ``content_size.width == 0``) ``None`` is passed as ``line_counts`` and
           the function falls back to the 1-line-per-row
           :func:`clamp_viewport_to_cursor` behaviour.
        3. Repaint.
        4. Post :class:`FileClicked` for the new ``_selected_index`` so the diff
           panel follows the cursor.

        Mouse wheel handlers use ``_scroll_mru`` instead — they pan the viewport
        without moving the cursor or posting FileClicked.
        """
        if not self._rows or self._last_model is None:
            return
        n = len(self._rows)
        self._selected_index = max(0, min(self._selected_index + delta, n - 1))
        content_width = self.content_size.width
        if content_width > 0:
            line_counts = [
                _wrapped_line_count(format_mru_row(entry), content_width)
                for entry in self._rows
            ]
        else:
            line_counts = None  # unmounted fallback: 1 line per row
        self._scroll_offset = viewport_offset_for_cursor(
            self._selected_index,
            self._scroll_offset,
            self._visible_rows(),
            n,
            line_counts=line_counts,
        )
        self._renderable = render_mru(
            self._last_model,
            self._scroll_offset,
            self._last_width,
            focused=self.has_focus,
        )
        if self.is_mounted:
            self.refresh()
        self._select_at_cursor()

    def watch_has_focus(self, has_focus: bool) -> None:
        """Re-highlight the title when keyboard focus enters/leaves this panel."""
        if self._last_model is None:
            return
        self._renderable = render_mru(
            self._last_model, self._scroll_offset, self._last_width, focused=has_focus
        )
        if self.is_mounted:
            self.refresh()

    def on_mouse_scroll_up(self, event) -> None:
        """Translate a wheel-up into a viewport pan (scroll-only, no cursor move)."""
        self._scroll_mru(-1)

    def on_mouse_scroll_down(self, event) -> None:
        """Translate a wheel-down into a viewport pan (scroll-only, no cursor move)."""
        self._scroll_mru(1)

    def _page_step(self) -> int:
        """Rows per page-scroll: delegates to _visible_rows() for single source of truth."""
        return self._visible_rows()

    def _select_at_cursor(self) -> None:
        """Post FileClicked for the entry at ``_selected_index`` (keyboard selection).

        Called after every keyboard navigation so ↑/↓/Page/Home/End both move
        the cursor AND pin the diff to the newly-highlighted entry — same effect
        as clicking a row with the mouse.  Mouse wheel handlers intentionally do
        NOT call this method (wheel is scroll-only).
        """
        if self._rows and 0 <= self._selected_index < len(self._rows):
            self.post_message(
                self.FileClicked(self._rows[self._selected_index].event_key)
            )

    def on_key(self, event) -> None:
        """↑/↓/PageUp/PageDown/Home/End move the cursor and follow the viewport."""
        if event.key == "up":
            self._navigate_cursor(-1)
            event.stop()
        elif event.key == "down":
            self._navigate_cursor(1)
            event.stop()
        elif event.key == "pagedown":
            self._navigate_cursor(self._page_step())
            event.stop()
        elif event.key == "pageup":
            self._navigate_cursor(-self._page_step())
            event.stop()
        elif event.key == "home":
            self._navigate_cursor(-_SCROLL_TO_EXTREME)
            event.stop()
        elif event.key == "end":
            self._navigate_cursor(_SCROLL_TO_EXTREME)
            event.stop()

    def on_mouse_down(self, event) -> None:
        """Map mouse-down Y → MRU row, accounting for wrapped long rows.

        Focuses the panel first so click-to-focus works, then maps the
        click to the appropriate MRU row and sets ``_selected_index`` so
        subsequent arrow keys move relative to the clicked row.

        Uses on_mouse_down instead of on_click because under tmux the button
        release event is not forwarded (tmux's root MouseDown1Pane binding
        sends the press but not the release), so Click is never generated.
        on_mouse_down fires on the press alone, which tmux does forward.

        Row heights are computed from content_size.width so that wrapped entries
        (long paths in a narrow panel) map correctly: each entry occupies
        ceil(len(plain_text) / content_width) physical lines, not 1.

        Iterates only the VISIBLE rows (offset by _scroll_offset) so a click
        maps to the correct entry when the list has been scrolled.
        """
        self.focus()
        _HEADER_LINES = 2  # title + blank line from render_mru()
        if event.offset.y < _HEADER_LINES or not self._rows:
            return
        content_width = self.content_size.width
        if content_width <= 0:
            return
        remaining = event.offset.y - _HEADER_LINES
        for loop_pos, entry in enumerate(self._rows[self._scroll_offset :]):
            plain = format_mru_row(entry)
            # integer ceil without math import: (a + b - 1) // b
            entry_lines = max(1, (len(plain) + content_width - 1) // content_width)
            if remaining < entry_lines:
                # Update _selected_index so arrow keys are relative to clicked row.
                self._selected_index = self._scroll_offset + loop_pos
                self.post_message(self.FileClicked(entry.event_key))
                return
            remaining -= entry_lines

    def render(self) -> Text:
        """Return the current Rich renderable (Textual repaints from this)."""
        return self._renderable

    def rendered_text(self) -> str:
        """Return the panel's current plain text (used by tests/assertions).

        Newlines are stripped so file-path substrings remain contiguous even
        when the chunked renderer breaks a long path across visual lines.
        """
        return self._renderable.plain.replace("\n", "")


# ---------------------------------------------------------------------------
# Diff panel (story #3) — pure renderers + widget
# ---------------------------------------------------------------------------


def shorten_model(model: Optional[str]) -> str:
    """Shorten a raw model id for the header, e.g. ``claude-opus-4-8`` → ``opus-4-8``.

    Strips the leading ``claude-`` vendor prefix when present; a label without
    the prefix passes through unchanged.  ``None`` / empty renders a stable
    placeholder so the header never collapses (AC3).
    """
    if not model:
        return _UNKNOWN_MODEL
    if model.startswith(_MODEL_PREFIX):
        return model[len(_MODEL_PREFIX) :]
    return model


def _basename(file_path: Optional[str]) -> str:
    """Filename portion of a path for the header (empty path → ``?``)."""
    if not file_path:
        return "?"
    return os.path.basename(file_path)


def format_diff_header(state: DisplayState) -> Text:
    """Build the Diff panel header: ``HH:MM:SS · model · 🧠 · filename · origin``.

    The header LEADS with the displayed file's ``HH:MM:SS`` modification time
    (or :data:`MISSING_TIME_TEXT` when the event carries no timestamp), formatted
    by the SHARED :func:`_format_time` helper — the same formatter the MRU rows
    and the Commands feed use, so the time reads identically across all three
    panels and never raises on ``None``.  The 🧠 glyph appears ONLY when
    ``state.used_thinking`` is true (AC3/AC4); the ``⤷sub`` marker only when the
    source is a subagent.  The model label is the short, ``claude-``-stripped
    form and the filename is the basename of the displayed path so the header
    stays compact.
    """
    header = Text()
    header.append(_format_time(state.ts), style="dim")
    header.append(HEADER_SEP)
    header.append(shorten_model(state.model), style="bold cyan")
    if state.used_thinking:
        header.append(HEADER_SEP)
        header.append(THINKING_GLYPH)
    header.append(HEADER_SEP)
    header.append(_basename(state.file_path), style="bold")
    header.append(HEADER_SEP)
    origin = f"{state.project_tag} {ORIGIN_SEP} {state.short_session}"
    header.append(origin, style="dim")
    if state.is_subagent:
        header.append(f"  {SUBAGENT_MARKER}", style="dim")
    return header


def render_diff_body(state: DisplayState) -> Text:
    """Render the visible diff segments coloured by kind, plus the ``+N more`` badge.

    Each segment in ``state.visible_segments`` is appended on its own line with
    the colour from :data:`~claude_visualizer.diffing.COLOR_FOR_KIND` for its
    kind (ADD→green, DEL→red, CONTEXT→dim, plus HEADER/TRUNCATION markers).
    When ``state.plus_n_more`` is non-zero a ``+N more`` badge is appended so an
    overflow/queued backlog is never silent (AC8); the truncation footer (AC10)
    is already present as a segment and rendered verbatim here.
    """
    body = Text()
    segments: List[DiffSegment] = list(state.visible_segments)
    for index, segment in enumerate(segments):
        # Gutter: 4-digit right-aligned line number + space, or 5 spaces blank.
        if segment.line_no is not None:
            body.append(f"{segment.line_no:4d} ", style="dim")
        else:
            body.append("     ", style="dim")
        style = COLOR_FOR_KIND.get(segment.kind, "")
        body.append(segment.text, style=style)
        if index < len(segments) - 1:
            body.append("\n")
    return body


def render_diff(
    state: Optional[DisplayState],
    *,
    focused: bool = False,
) -> Text:
    """Render the whole Diff panel: title + header + coloured body.

    A ``None`` state (nothing ever shown) or an empty state (no file_path /
    segments) renders the titled waiting message — the panel never blanks
    (AC7-adjacent).  Otherwise the header (model · 🧠 · filename · origin) sits
    above the colour-mapped diff body with its ``+N more`` badge.

    ``focused`` when True renders the title with ``bold reverse`` so the panel
    title highlights to indicate keyboard focus (no border change, no layout
    shift).
    """
    out = Text()
    pinned = state is not None and getattr(state, "is_pinned", False)
    title = "Live Diff — 📌 pinned" if pinned else DIFF_TITLE
    title_style = "bold reverse" if focused else "bold"
    out.append(f"{title}\n", style=title_style)
    if state is None or state.file_path is None:
        out.append(f"\n{DIFF_EMPTY_TEXT}")
        return out
    out.append(format_diff_header(state))
    out.append("\n\n")
    out.append(render_diff_body(state))
    return out


class DiffPanel(Static):
    """Active top-right panel: the live, colour-mapped diff of the current file.

    A pure view over an immutable :class:`DisplayState` produced by the
    ``DiffQueueModel`` (which owns ALL timing/scroll/advance).  ``update_from_state``
    re-renders from the supplied snapshot; it performs no IO and no diffing on
    the render path — the model already computed the visible window.  The
    rendered value is a Rich :class:`~rich.text.Text` so colours show on screen;
    ``rendered_text`` exposes its plain string for substring assertions.

    ``can_focus = True`` so Tab/Shift+Tab cycle through the three panels.
    ↑/↓ keys post :class:`DiffScrolled` messages (no-op when unpinned).
    When the diff is pinned, mouse-wheel events post a :class:`DiffScrolled`
    message that the app handles by calling ``DiffQueueModel.scroll_pin_by()``.
    Non-pinned diffs auto-scroll via the queue's dwell logic; the message is
    posted unconditionally and the app/model decide whether to act on it.
    """

    can_focus = True
    ALLOW_SELECT = False

    class DiffScrolled(Message):
        """Posted when the user scrolls the diff panel with the mouse wheel.

        ``delta`` is positive for scroll-down (content moves up, reveals later
        lines) and negative for scroll-up (content moves down, reveals earlier
        lines).  The app forwards this to ``DiffQueueModel.scroll_pin_by()``,
        which is a no-op when not pinned, so the message is always safe to post.
        """

        def __init__(self, delta: int) -> None:
            super().__init__()
            self.delta = delta

    def __init__(self, **kwargs) -> None:
        super().__init__(DIFF_EMPTY_TEXT, **kwargs)
        self._renderable: Text = render_diff(None)
        self._last_state: Optional[DisplayState] = None

    def update_from_state(self, state: Optional[DisplayState]) -> None:
        """Re-render the panel from the supplied display state (or None)."""
        self._last_state = state
        self._renderable = render_diff(state, focused=self.has_focus)
        if self.is_mounted:
            self.refresh()

    def watch_has_focus(self, has_focus: bool) -> None:
        self._renderable = render_diff(self._last_state, focused=has_focus)
        if self.is_mounted:
            self.refresh()

    def on_mouse_down(self, event) -> None:
        """Focus this panel on mouse-down so click-to-focus works."""
        self.focus()

    def on_mouse_scroll_up(self, event) -> None:
        """Translate a wheel-up into a DiffScrolled(-1) message."""
        self.post_message(self.DiffScrolled(-1))

    def on_mouse_scroll_down(self, event) -> None:
        """Translate a wheel-down into a DiffScrolled(+1) message."""
        self.post_message(self.DiffScrolled(1))

    def _page_step(self) -> int:
        """Rows per page-scroll: content height minus title chrome, floored at 1."""
        return max(1, self.content_size.height - _PANEL_TITLE_CHROME)

    def on_key(self, event) -> None:
        """↑/↓/PageUp/PageDown post DiffScrolled messages (no-op when unpinned via model)."""
        if event.key == "up":
            self.post_message(self.DiffScrolled(-1))
            event.stop()
        elif event.key == "down":
            self.post_message(self.DiffScrolled(1))
            event.stop()
        elif event.key == "pagedown":
            self.post_message(self.DiffScrolled(self._page_step()))
            event.stop()
        elif event.key == "pageup":
            self.post_message(self.DiffScrolled(-self._page_step()))
            event.stop()
        elif event.key == "home":
            self.post_message(self.DiffScrolled(-_SCROLL_TO_EXTREME))
            event.stop()
        elif event.key == "end":
            self.post_message(self.DiffScrolled(_SCROLL_TO_EXTREME))
            event.stop()

    def render(self) -> Text:
        """Return the current Rich renderable (Textual repaints from this)."""
        return self._renderable

    def rendered_text(self) -> str:
        """Return the panel's current plain text (used by tests/assertions)."""
        return self._renderable.plain


# ---------------------------------------------------------------------------
# Commands feed (story #4) — pure renderers + widget
# ---------------------------------------------------------------------------


def scroll_command(text: str, width: int, h_scroll: int) -> str:
    """Render a command with horizontal scrolling, fitting ``width`` characters.

    Collapses newlines, applies a per-row horizontal scroll offset, and adds
    ellipsis indicators so the user can see which direction hidden text lies:

    - ``width <= 0`` → empty string.
    - ``effective_scroll = min(h_scroll, max(0, len(single_line) - width))``
      (the raw ``h_scroll`` is clamped so you cannot scroll past the last visible
      character).
    - If the remaining visible slice is still longer than ``width``, it is
      truncated with a trailing ``…`` (text extends to the right).
    - If ``effective_scroll > 0`` the first character of the result is replaced
      with ``…`` (text hidden to the left).

    This means both indicators can appear simultaneously when the command is
    long enough that neither end is visible in the current window.
    """
    if width <= 0:
        return ""
    single_line = " ".join(text.split("\n"))
    effective = min(max(0, h_scroll), max(0, len(single_line) - width))
    visible = single_line[effective:]
    # Truncate right if still too long
    if len(visible) > width:
        if width == 1:
            return TRUNCATION_ELLIPSIS
        visible = visible[: width - 1] + TRUNCATION_ELLIPSIS
    # Add left ellipsis if scrolled (replace first char to keep total width)
    if effective > 0:
        if len(visible) == 0:
            return TRUNCATION_ELLIPSIS
        visible = TRUNCATION_ELLIPSIS + visible[1:]
    return visible


def truncate_command(text: str, width: int) -> str:
    """Fit a command string onto ONE row of ``width`` characters (AC3).

    A command may legitimately contain newlines (heredocs, multi-line scripts);
    those are collapsed to single spaces first so the command never spills onto
    extra feed rows.  The result is then truncated to ``width`` with a trailing
    :data:`TRUNCATION_ELLIPSIS` when it would otherwise overflow:

    - ``width <= 0`` → empty string (nothing fits).
    - ``len <= width`` → returned unchanged.
    - otherwise keep the leading ``width - 1`` characters and append ``…`` so the
      returned width is exactly ``width`` (``width == 1`` → just the ellipsis).
    """
    if width <= 0:
        return ""
    single_line = " ".join(text.split("\n"))
    if len(single_line) <= width:
        return single_line
    if width == 1:
        return TRUNCATION_ELLIPSIS
    return single_line[: width - 1] + TRUNCATION_ELLIPSIS


def _format_time(ts: Optional[datetime]) -> str:
    """Render a command's timestamp as ``HH:MM:SS`` (or a stable placeholder).

    A ``None`` timestamp (synthetic/un-timestamped event) yields
    :data:`MISSING_TIME_TEXT` so the row still aligns and never raises — this is
    a display-only fail-soft, not a data fallback (the command/origin are real).
    """
    if ts is None:
        return MISSING_TIME_TEXT
    return ts.strftime(TIME_FORMAT)


def format_command_row(entry: CommandFeedEntry, width: int, h_scroll: int = 0) -> str:
    """Render one command-feed entry as a single line that fits ``width`` (AC3).

    Layout: ``<time>  <command>  <project> · <short_session>`` with a trailing
    ``⤷sub`` marker when the source is a subagent.  The TIME is the fixed-width
    leftmost prefix; the COMMAND is the flexible middle field rendered by
    :func:`scroll_command` (which handles both horizontal scrolling and right
    truncation) to whatever space remains after reserving room for the prefix
    and the origin suffix; the ORIGIN is the fixed right suffix.  As a final
    backstop the assembled row is itself passed through :func:`truncate_command`
    so the WHOLE row is guaranteed to fit ``width`` even on a panel so narrow
    that the prefix + suffix alone would overflow.  The row is single-line.

    ``h_scroll`` offsets the command text window to the right (revealing the
    tail of a long command); the origin suffix always stays fixed.
    """
    time_str = _format_time(entry.ts)
    origin = f"{entry.project_tag} {ORIGIN_SEP} {entry.short_session}"
    time_prefix = f"{time_str}  "
    origin_suffix = f"  {origin}"
    if entry.is_subagent:
        origin_suffix = f"{origin_suffix}  {SUBAGENT_MARKER}"
    # The command gets the remaining width after the time prefix and origin
    # suffix are reserved.  If those alone already meet or exceed the panel
    # width the command field collapses to empty (origin is higher-value).
    # The final truncate is the backstop that makes ``len(row) <= width``
    # hold unconditionally.
    command_width = max(0, width - len(time_prefix) - len(origin_suffix))
    command = scroll_command(entry.command, command_width, h_scroll)
    return truncate_command(f"{time_prefix}{command}{origin_suffix}", width)


def _styled_command_row(entry: CommandFeedEntry, width: int, h_scroll: int = 0) -> Text:
    """Build a command-feed row as a Rich Text with per-field color accents.

    The command text is the main content (no style applied); all metadata
    fields (timestamp, project tag, separator, session, subagent marker) carry
    subtle ``dim`` tints so they recede visually without disappearing.

    ``h_scroll`` offsets the command text window; origin fields are always fixed.
    The leading ``…`` added by :func:`scroll_command` when scrolled carries no
    special style (same as the command text).
    """
    time_str = _format_time(entry.ts)
    time_prefix = f"{time_str}  "
    origin_parts_plain = f"  {entry.project_tag} {ORIGIN_SEP} {entry.short_session}"
    if entry.is_subagent:
        origin_parts_plain += f"  {SUBAGENT_MARKER}"
    # Scroll/truncate the command to fit remaining width.
    command_width = max(0, width - len(time_prefix) - len(origin_parts_plain))
    command = scroll_command(entry.command, command_width, h_scroll)

    row = Text()
    row.append(time_str, style="dim")
    row.append("  ")
    row.append(command)  # default — main content, no dim
    row.append("  ")
    row.append(entry.project_tag, style="dim green")
    row.append(f" {ORIGIN_SEP} ", style="dim")
    row.append(entry.short_session, style="dim magenta")
    if entry.is_subagent:
        row.append("  ")
        row.append(SUBAGENT_MARKER, style="dim yellow")

    # Final width backstop: truncate the whole Text if it's still too wide.
    if len(row) > width > 0:
        row.truncate(width - 1)
        row.append(TRUNCATION_ELLIPSIS)
    return row


def render_commands(
    model: CommandFeedModel,
    scroll_offset: int,
    width: int,
    *,
    focused: bool = False,
    h_scroll: int = 0,
) -> Text:
    """Render the command feed newest-on-top as a Rich block (AC1/AC2).

    ``scroll_offset`` skips the first N rows (rows are newest-first, so offset 0
    shows the newest at top; a positive offset reveals older commands).  Keeping
    offset at 0 and updating on every new record gives the autoscroll-follow
    behaviour; a non-zero offset lets the user "hold" a position in history.

    Returns a waiting message (still inside the titled block) when the feed is
    empty or the offset has scrolled past all rows, so the panel never shows a
    blank void before any command is observed.  Each entry is rendered by
    :func:`_styled_command_row` (when ``width > 0``) or :func:`format_command_row`
    (fallback for width ≤ 0); the feed is a LOG so identical commands each get
    their own row (no dedup).

    ``focused`` when True renders the title with ``bold reverse`` so the panel
    title highlights to indicate keyboard focus (no border change, no layout
    shift).

    ``h_scroll`` offsets the command text in every visible row; the per-row
    clamping in :func:`scroll_command` ensures short commands are unaffected.
    """
    out = Text()
    title_style = "bold reverse" if focused else "bold"
    out.append(f"{COMMANDS_TITLE}\n\n", style=title_style)
    rows = model.rows()
    if not rows:
        out.append(COMMANDS_EMPTY_TEXT)
        return out
    visible = rows[scroll_offset:]
    if not visible:
        out.append(COMMANDS_EMPTY_TEXT)
        return out
    for index, entry in enumerate(visible):
        if width > 0:
            out.append_text(_styled_command_row(entry, width, h_scroll))
        else:
            out.append(format_command_row(entry, width, h_scroll))
        if index < len(visible) - 1:
            out.append("\n")
    return out


class CommandsPanel(Static):
    """Bottom panel: a live, newest-on-top rolling feed of Bash commands.

    A pure view over a :class:`CommandFeedModel`.  ``update_from_model``
    re-renders from the model's current rows at the supplied panel ``width``; it
    performs no IO and never parses transcripts — the pipeline owns all of that
    and only feeds the model.  The rendered value is a Rich
    :class:`~rich.text.Text`; ``rendered_text`` exposes its plain string for
    substring assertions in tests.

    ``can_focus = True`` so Tab/Shift+Tab cycle through the three panels.
    ↑/↓ keys scroll the visible row window; the panel follows the newest command
    automatically (``_follow = True``) until the user scrolls away.
    """

    can_focus = True
    ALLOW_SELECT = False

    def __init__(self, **kwargs) -> None:
        super().__init__(COMMANDS_EMPTY_TEXT, **kwargs)
        self._renderable: Text = Text(COMMANDS_EMPTY_TEXT)
        self._scroll_offset: int = 0
        self._follow: bool = True
        self._last_model: Optional[CommandFeedModel] = None
        self._last_width: int = 0
        self._h_scroll_offset: int = 0
        self._last_row_count: int = 0

    def update_from_model(self, model: CommandFeedModel, width: int) -> None:
        """Re-render the panel from the model's current rows at ``width``.

        When ``_follow`` is True the scroll offset is reset to 0 so the newest
        command stays visible (autoscroll-follow), and ``_h_scroll_offset`` is
        also reset to 0 so a new command always starts at the left edge.  When
        ``_follow`` is False (user has manually scrolled) both offsets are
        preserved so the user is not yanked away from their current view.
        """
        self._last_model = model
        self._last_width = width
        rows = model.rows()
        row_count = len(rows)
        if self._follow:
            self._scroll_offset = 0
            if row_count != self._last_row_count:
                self._h_scroll_offset = 0
        else:
            self._scroll_offset = min(self._scroll_offset, max(0, row_count - 1))
        self._last_row_count = row_count
        self._renderable = render_commands(
            model,
            self._scroll_offset,
            width,
            focused=self.has_focus,
            h_scroll=self._h_scroll_offset,
        )
        if self.is_mounted:
            self.refresh()

    def _scroll_commands(self, delta: int) -> None:
        """Adjust ``_scroll_offset`` by ``delta`` and repaint from stored model.

        Clamped so the offset is always in [0, len(rows)-1].  When the offset
        returns to 0 ``_follow`` is re-enabled so the next new command snaps
        back to the top (autoscroll-follow resumes).
        """
        if self._last_model is None:
            return
        rows = self._last_model.rows()
        self._scroll_offset = max(
            0, min(self._scroll_offset + delta, max(0, len(rows) - 1))
        )
        self._follow = self._scroll_offset == 0
        self._renderable = render_commands(
            self._last_model,
            self._scroll_offset,
            self._last_width,
            focused=self.has_focus,
            h_scroll=self._h_scroll_offset,
        )
        if self.is_mounted:
            self.refresh()

    def _h_scroll_commands(self, delta: int) -> None:
        """Adjust ``_h_scroll_offset`` by ``delta`` and repaint.

        The panel-level offset is a "desired" offset; per-row clamping in
        :func:`scroll_command` handles the upper bound so short commands are
        never over-scrolled.  The floor is 0 — you cannot scroll left of the
        start of a command.
        """
        self._h_scroll_offset = max(0, self._h_scroll_offset + delta)
        if self._last_model is None:
            return
        self._renderable = render_commands(
            self._last_model,
            self._scroll_offset,
            self._last_width,
            focused=self.has_focus,
            h_scroll=self._h_scroll_offset,
        )
        if self.is_mounted:
            self.refresh()

    def watch_has_focus(self, has_focus: bool) -> None:
        if self._last_model is None:
            return
        self._renderable = render_commands(
            self._last_model,
            self._scroll_offset,
            self._last_width,
            focused=has_focus,
            h_scroll=self._h_scroll_offset,
        )
        if self.is_mounted:
            self.refresh()

    def on_mouse_down(self, event) -> None:
        """Focus this panel on mouse-down so click-to-focus works."""
        self.focus()

    def on_mouse_scroll_up(self, event) -> None:
        """Translate a wheel-up into a scroll-up (reveal newer / top rows)."""
        self._scroll_commands(-1)

    def on_mouse_scroll_down(self, event) -> None:
        """Translate a wheel-down into a scroll-down (reveal older rows)."""
        self._scroll_commands(1)

    def _page_step(self) -> int:
        """Rows per page-scroll: content height minus title chrome, floored at 1."""
        return max(1, self.content_size.height - _PANEL_TITLE_CHROME)

    def on_key(self, event) -> None:
        """↑/↓/PageUp/PageDown scroll the Commands row window; ←/→ scroll command text."""
        if event.key == "up":
            self._scroll_commands(-1)
            event.stop()
        elif event.key == "down":
            self._scroll_commands(1)
            event.stop()
        elif event.key == "pagedown":
            self._scroll_commands(self._page_step())
            event.stop()
        elif event.key == "pageup":
            self._scroll_commands(-self._page_step())
            event.stop()
        elif event.key == "home":
            self._scroll_commands(-_SCROLL_TO_EXTREME)
            event.stop()
        elif event.key == "end":
            self._scroll_commands(_SCROLL_TO_EXTREME)
            event.stop()
        elif event.key == "left":
            # Slide command text LEFT → reveal more of the tail (increase offset)
            self._h_scroll_commands(1)
            event.stop()
        elif event.key == "right":
            # Slide command text RIGHT → reveal more of the start (decrease offset)
            self._h_scroll_commands(-1)
            event.stop()

    def render(self) -> Text:
        """Return the current Rich renderable (Textual repaints from this)."""
        return self._renderable

    def rendered_text(self) -> str:
        """Return the panel's current plain text (used by tests/assertions)."""
        return self._renderable.plain


# ---------------------------------------------------------------------------
# Monitor bar — pluggable per-monitor stacked rows (story #6)
# ---------------------------------------------------------------------------

# Fallback content width for the MonitorBar before the first layout measures it,
# so the very first repaint truncates rows to a sensible width (same pattern as
# the Commands panel's _COMMANDS_DEFAULT_WIDTH in app.py).
_MONITOR_BAR_DEFAULT_WIDTH = 120


def _filter_active_monitor_lines(lines: list) -> list:
    """Return only non-empty entries from a monitor tick result list.

    A line is considered active (non-empty) when:
    - it is a ``str`` with at least one character, or
    - it is a ``rich.text.Text`` whose ``.plain`` is non-empty.

    Empty strings and ``Text("")`` are suppressed-monitor entries; callers
    should hide those rows rather than rendering a blank line.
    """
    active: list = []
    for line in lines:
        if isinstance(line, Text):
            if line.plain:
                active.append(line)
        elif isinstance(line, str):
            if line:
                active.append(line)
        elif line:
            # N2: a misbehaving plugin may return a truthy non-str/non-Text
            # value (e.g. an int). Coerce to str so render_monitor_bar can never
            # raise TypeError on a refresh tick — same fault-isolation spirit as
            # AC5. Falsy non-str values stay suppressed.
            active.append(str(line))
    return active


def render_monitor_bar(lines: list, width: Optional[int] = None) -> Text:
    """Render a list of monitor lines as a stacked Rich block.

    Skips any entry that is an empty string or a ``Text`` whose ``.plain``
    is empty — those are suppressed monitors (AC2).  The result contains only
    non-empty lines joined by newlines.  NEVER appends an empty trailing line
    (``Text("")`` appended to a ``Text`` object costs 1 display row — the
    Textual height gotcha).

    When ``width`` is a positive int, each active line is truncated to at most
    ``width`` cells with a trailing ``…`` ellipsis BEFORE joining.  This is the
    actual fix for the Textual wrapping bug: Rich's ``Text.no_wrap`` flag is
    ignored by Textual's compositor, but a line that is already ≤ width cells
    cannot wrap regardless.  Use ``MonitorBar.update_from_lines`` which passes
    ``self.content_size.width`` so lines are guaranteed ≤ the widget's content
    width and ``height: auto`` resolves to exactly the monitor count.

    When ``width`` is ``None`` (the default) no truncation is applied —
    existing callers and tests are unaffected.
    """
    active = _filter_active_monitor_lines(lines)
    out = Text(no_wrap=True, overflow="ellipsis")
    for i, line in enumerate(active):
        # Build a per-line Text object so we can truncate it cell-accurately.
        if isinstance(line, Text):
            t = line.copy()
        else:
            t = Text(str(line))
        # Truncate to width cells with Rich's cell-aware truncate when requested.
        # This is the REAL fix: each line ≤ width → Textual cannot wrap it.
        if width is not None and width > 0:
            t.truncate(width, overflow="ellipsis")
        out.append_text(t)
        if i < len(active) - 1:
            out.append("\n")
    return out


class MonitorBar(Static):
    """Docked bottom bar showing one row per active monitor.

    ``update_from_lines`` accepts the list returned by ``MonitorRegistry.tick()``,
    filters out suppressed (empty) monitors, and either shows the stacked rows
    or sets ``display = False`` when ALL monitors are suppressed (AC3).

    ``DEFAULT_CSS`` docks at the bottom with ``height: auto`` so the bar height
    equals the number of active monitor rows automatically (AC3).
    """

    ALLOW_SELECT = False

    DEFAULT_CSS = """
    MonitorBar {
        dock: bottom;
        height: auto;
        background: #2d2420;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__("", **kwargs)
        self._renderable: Text = Text("")

    def update_from_lines(self, lines: list) -> None:
        """Re-render from monitor tick results; collapse to invisible when all empty.

        Computes the available content width from ``self.content_size.width``
        (Textual's measured content region, excluding border/padding) and passes
        it to :func:`render_monitor_bar` so each active line is truncated to at
        most that many cells before joining.  This prevents Textual from wrapping
        long lines inside a ``height: auto`` widget, keeping
        ``MonitorBar.size.height`` equal to the active monitor count.

        Before the first layout ``content_size.width`` is 0 (unmeasured);
        :data:`_MONITOR_BAR_DEFAULT_WIDTH` is used as a pre-layout fallback —
        the periodic refresh tick re-paints with the real width once layout is
        complete (same pattern as the Commands panel's ``_COMMANDS_DEFAULT_WIDTH``
        in ``app.py``).
        """
        active = _filter_active_monitor_lines(lines)
        if not active:
            self.display = False
        else:
            self.display = True
            width = (
                self.content_size.width
                if self.content_size.width > 0
                else _MONITOR_BAR_DEFAULT_WIDTH
            )
            self._renderable = render_monitor_bar(lines, width=width)
            if self.is_mounted:
                # layout=True is required so Textual re-measures content height for
                # the ``height: auto`` widget.  A plain refresh() (layout=False) repaints
                # the content but does NOT trigger a layout pass, so size.height stays
                # stale at the pre-truncation measurement.  Static.update() uses the
                # same layout=True pattern for the same reason.
                self.refresh(layout=True)

    def render(self) -> Text:
        """Return the current Rich renderable (Textual repaints from this)."""
        return self._renderable

    def rendered_text(self) -> str:
        """Return the panel's current plain text (used by tests/assertions)."""
        return self._renderable.plain


class SplitterHandle(Widget):
    """Thin │ vertical line between MRU and Diff panels — draggable splitter.

    Renders a column of │ box-drawing characters on the default background so
    it appears as a thin line rather than a filled colour block.  Arrow-key
    resizing lives in app-level bindings; mouse drag fires :class:`SplitterDragged`
    so the app can adjust the MRU panel width in real time.
    """

    class SplitterDragged(Message):
        """Posted while the user drags the vertical splitter handle.

        ``delta_x`` is the signed horizontal movement in screen columns since
        the last event (positive = moved right, negative = moved left).  The
        app adds this to ``_mru_width`` and clamps at ``_MRU_MIN_WIDTH``.
        """

        def __init__(self, delta_x: int) -> None:
            super().__init__()
            self.delta_x = delta_x

    DEFAULT_CSS = """
    SplitterHandle {
        width: 1;
        height: 100%;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._dragging = False
        self._drag_start_x = 0

    def on_mouse_down(self, event) -> None:
        """Begin drag: capture mouse so move/up events arrive even outside bounds."""
        self._dragging = True
        self._drag_start_x = event.screen_x
        self.capture_mouse()
        event.prevent_default()
        event.stop()

    def on_mouse_move(self, event) -> None:
        """Fire SplitterDragged for each column moved during a drag."""
        if self._dragging:
            delta = event.screen_x - self._drag_start_x
            if delta != 0:
                self.post_message(self.SplitterDragged(delta))
                self._drag_start_x = event.screen_x
            event.prevent_default()
            event.stop()

    def on_mouse_up(self, event) -> None:
        """End drag: release mouse capture."""
        if self._dragging:
            self._dragging = False
            self.release_mouse()
            event.prevent_default()
            event.stop()

    def render(self) -> Text:
        height = max(1, self.size.height)
        return Text("\n".join(["│"] * height), style="dim")


class HorizontalSeparator(Widget):
    """Thin ─ horizontal line between the top panels and the bottom Commands panel.

    Renders a row of ─ box-drawing characters on the default background so it
    appears as a thin line rather than a filled colour block.  Mouse drag fires
    :class:`SeparatorDragged` so the app can adjust the Commands panel height.
    """

    class SeparatorDragged(Message):
        """Posted while the user drags the horizontal separator.

        ``delta_y`` is the signed vertical movement in screen rows since
        the last event (positive = moved down, negative = moved up).  The
        app subtracts this from ``_bottom_height`` and clamps at
        ``_BOTTOM_MIN_HEIGHT``.
        """

        def __init__(self, delta_y: int) -> None:
            super().__init__()
            self.delta_y = delta_y

    DEFAULT_CSS = """
    HorizontalSeparator {
        height: 1;
        width: 100%;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._dragging = False
        self._drag_start_y = 0

    def on_mouse_down(self, event) -> None:
        """Begin drag: capture mouse so move/up events arrive even outside bounds."""
        self._dragging = True
        self._drag_start_y = event.screen_y
        self.capture_mouse()
        event.prevent_default()
        event.stop()

    def on_mouse_move(self, event) -> None:
        """Fire SeparatorDragged for each row moved during a drag."""
        if self._dragging:
            delta = event.screen_y - self._drag_start_y
            if delta != 0:
                self.post_message(self.SeparatorDragged(delta))
                self._drag_start_y = event.screen_y
            event.prevent_default()
            event.stop()

    def on_mouse_up(self, event) -> None:
        """End drag: release mouse capture."""
        if self._dragging:
            self._dragging = False
            self.release_mouse()
            event.prevent_default()
            event.stop()

    def render(self) -> Text:
        width = max(1, self.size.width)
        return Text("─" * width, style="dim")
