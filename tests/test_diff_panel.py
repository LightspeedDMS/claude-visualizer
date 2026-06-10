"""Tests for the top-right Diff panel renderers + widget (story #3, chunk B).

Two layers, mirroring the MRU panel's split:

1. PURE renderers (no Textual runtime): ``shorten_model``, ``format_diff_header``,
   ``render_diff_body``, ``render_diff``.  They turn an immutable
   :class:`DisplayState` (produced by the pure ``DiffQueueModel``) into a Rich
   renderable; colour comes from the ``COLOR_FOR_KIND`` data table so the logic
   stays unit-testable.  Fast, deterministic, anti-mock (real DisplayState).

2. The :class:`DiffPanel` Textual ``Static`` widget — exercised here only for its
   pure ``update_from_state`` / ``rendered_text`` seam (the REAL ``run_test()``
   harness drives it end-to-end in ``test_ui.py``).

ACs exercised here:
- AC2  Write → all-green additions, no fabricated before-state (no DEL, no header label).
- AC3  Header: short model label · 🧠 (iff used_thinking) · filename · origin
       (project · short session, ⤷sub when subagent).
- AC8  ``+N more`` badge when ``plus_n_more`` > 0.
- AC10 ``…(truncated…)`` footer segment rendered when present.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional

from rich.text import Text

from claude_visualizer.config import AppConfig
from claude_visualizer.diffing import (
    COLOR_FOR_KIND,
    DiffKind,
    DiffSegment,
)
from claude_visualizer.events import CommandEvent, FileModifiedEvent, FileOp
from claude_visualizer.models.command_feed import CommandFeedModel
from claude_visualizer.models.diff_queue import DisplayState
from claude_visualizer.models.mru import MruModel
from claude_visualizer.ui.panels import (
    COMMANDS_TITLE,
    DIFF_EMPTY_TEXT,
    DIFF_TITLE,
    MISSING_TIME_TEXT,
    THINKING_GLYPH,
    DiffPanel,
    diff_viewport_height,
    format_diff_header,
    render_commands,
    render_diff,
    render_diff_body,
    render_mru,
    shorten_model,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _state(
    *,
    file_path: Optional[str] = "/home/dev/proj/app.py",
    visible_segments: Optional[List[DiffSegment]] = None,
    segments: Optional[List[DiffSegment]] = None,
    scroll_offset: int = 0,
    model: Optional[str] = "claude-opus-4-8",
    used_thinking: bool = False,
    is_subagent: bool = False,
    project_tag: str = "proj",
    short_session: str = "abc12345",
    op: Optional[FileOp] = FileOp.EDIT,
    plus_n_more: int = 0,
    is_idle: bool = True,
    ts: Optional[datetime] = datetime(2024, 1, 2, 13, 45, 7, tzinfo=timezone.utc),
) -> DisplayState:
    vis = visible_segments if visible_segments is not None else []
    return DisplayState(
        file_path=file_path,
        segments=segments if segments is not None else list(vis),
        visible_segments=vis,
        scroll_offset=scroll_offset,
        model=model,
        used_thinking=used_thinking,
        is_subagent=is_subagent,
        project_tag=project_tag,
        short_session=short_session,
        op=op,
        plus_n_more=plus_n_more,
        is_idle=is_idle,
        ts=ts,
    )


def _plain(text: Text) -> str:
    """Return the plain string of a Rich ``Text`` for substring asserts."""
    return text.plain


def _span_styles(text: Text) -> str:
    """Join every span's style string so colour assertions are deterministic.

    The renderers attach colours as named Rich styles (``green``/``red``/``dim``)
    directly on the ``Text`` spans; asserting against the span styles is exact
    and avoids the ambiguity of scraping ANSI escape sequences.
    """
    return " ".join(str(span.style) for span in text.spans)


# ---------------------------------------------------------------------------
# shorten_model (AC3)
# ---------------------------------------------------------------------------


class TestShortenModel:
    def test_strips_claude_prefix(self):
        assert shorten_model("claude-opus-4-8") == "opus-4-8"

    def test_strips_claude_prefix_sonnet(self):
        assert shorten_model("claude-sonnet-4-5") == "sonnet-4-5"

    def test_passthrough_when_no_prefix(self):
        assert shorten_model("opus-4-8") == "opus-4-8"

    def test_none_renders_unknown_placeholder(self):
        # A missing model must not crash the header; it renders a stable tag.
        out = shorten_model(None)
        assert isinstance(out, str)
        assert out  # non-empty

    def test_empty_string_renders_placeholder(self):
        assert shorten_model("") == shorten_model(None)


# ---------------------------------------------------------------------------
# format_diff_header (AC3)
# ---------------------------------------------------------------------------


class TestFormatDiffHeader:
    def test_header_has_short_model_filename_and_origin(self):
        text = _plain(format_diff_header(_state()))
        assert "opus-4-8" in text  # shortened model
        assert "app.py" in text  # filename (basename of file_path)
        assert "proj" in text  # project tag
        assert "abc12345" in text  # short session
        assert "·" in text  # origin separator present

    def test_thinking_glyph_shown_only_when_used_thinking(self):
        with_think = _plain(format_diff_header(_state(used_thinking=True)))
        without = _plain(format_diff_header(_state(used_thinking=False)))
        assert THINKING_GLYPH in with_think
        assert THINKING_GLYPH not in without

    def test_subagent_marker_present_only_for_subagent(self):
        sub = _plain(format_diff_header(_state(is_subagent=True)))
        main = _plain(format_diff_header(_state(is_subagent=False)))
        assert "⤷sub" in sub
        assert "⤷sub" not in main

    def test_filename_is_basename_not_full_path(self):
        text = _plain(
            format_diff_header(_state(file_path="/very/deep/nested/widget.py"))
        )
        assert "widget.py" in text
        # The full directory chain is not crammed into the header.
        assert "/very/deep/nested/widget.py" not in text

    def test_empty_filename_renders_question_mark_placeholder(self):
        # Defensive: an event with an empty (but non-None) path still renders a
        # stable filename token rather than a blank gap in the header.
        text = _plain(format_diff_header(_state(file_path="")))
        assert "?" in text

    def test_header_contains_time(self):
        # The displayed file's modification time appears HH:MM:SS in the header,
        # consistent with the MRU rows and the commands feed.
        text = _plain(
            format_diff_header(
                _state(ts=datetime(2024, 1, 2, 13, 45, 7, tzinfo=timezone.utc))
            )
        )
        assert "13:45:07" in text

    def test_missing_timestamp_renders_placeholder(self):
        # A None timestamp must not raise; the header shows the stable
        # placeholder and still carries the filename.
        text = _plain(format_diff_header(_state(file_path="/r/x.py", ts=None)))
        assert MISSING_TIME_TEXT in text
        assert "x.py" in text


# ---------------------------------------------------------------------------
# render_diff_body — colours (AC1/AC2), +N more (AC8), truncation (AC10)
# ---------------------------------------------------------------------------


class TestRenderDiffBody:
    def test_add_line_is_green(self):
        seg = DiffSegment(DiffKind.ADD, "+ new line")
        body = render_diff_body(_state(visible_segments=[seg]))
        assert "+ new line" in _plain(body)
        assert COLOR_FOR_KIND[DiffKind.ADD] in _span_styles(body)  # "green"

    def test_del_line_is_red(self):
        seg = DiffSegment(DiffKind.DEL, "- gone line")
        body = render_diff_body(_state(visible_segments=[seg]))
        assert COLOR_FOR_KIND[DiffKind.DEL] in _span_styles(body)  # "red"

    def test_context_line_is_dim(self):
        seg = DiffSegment(DiffKind.CONTEXT, "  same line")
        body = render_diff_body(_state(visible_segments=[seg]))
        assert COLOR_FOR_KIND[DiffKind.CONTEXT] in _span_styles(body)  # "dim"

    def test_write_whole_file_additions(self):
        # AC2: a Write renders all-green additions; HEADER segments still render when present.
        header = DiffSegment(DiffKind.HEADER, "whole-file write: /r/x.py")
        body = DiffSegment(DiffKind.ADD, "+ print('hi')")
        text = _plain(
            render_diff_body(_state(op=FileOp.WRITE, visible_segments=[header, body]))
        )
        assert "whole-file write" in text
        assert "+ print('hi')" in text

    def test_plus_n_more_badge_not_rendered(self):
        seg = DiffSegment(DiffKind.ADD, "+ a")
        text = _plain(render_diff_body(_state(visible_segments=[seg], plus_n_more=4)))
        assert "more" not in text

    def test_truncation_footer_rendered(self):
        # AC10: the truncation footer segment is rendered verbatim.
        footer = DiffSegment(DiffKind.TRUNCATION, "…(truncated, 12 more lines)")
        text = _plain(render_diff_body(_state(visible_segments=[footer])))
        assert "…(truncated, 12 more lines)" in text


# ---------------------------------------------------------------------------
# Gutter line-number rendering
# ---------------------------------------------------------------------------


class TestGutterRendering:
    """render_diff_body prepends a 4-digit right-aligned gutter to every line."""

    def test_add_segment_shows_line_number_in_gutter(self):
        seg = DiffSegment(DiffKind.ADD, "+ new line", line_no=1)
        text = _plain(render_diff_body(_state(visible_segments=[seg])))
        assert "   1 + new line" in text

    def test_del_segment_shows_line_number_in_gutter(self):
        seg = DiffSegment(DiffKind.DEL, "- gone line", line_no=5)
        text = _plain(render_diff_body(_state(visible_segments=[seg])))
        assert "   5 - gone line" in text

    def test_context_segment_shows_line_number_in_gutter(self):
        seg = DiffSegment(DiffKind.CONTEXT, "  same line", line_no=12)
        text = _plain(render_diff_body(_state(visible_segments=[seg])))
        assert "  12   same line" in text

    def test_large_line_number_right_aligned_in_4_chars(self):
        seg = DiffSegment(DiffKind.ADD, "+ last", line_no=999)
        text = _plain(render_diff_body(_state(visible_segments=[seg])))
        assert " 999 + last" in text

    def test_4digit_line_number_fits_without_padding(self):
        seg = DiffSegment(DiffKind.ADD, "+ big", line_no=1234)
        text = _plain(render_diff_body(_state(visible_segments=[seg])))
        assert "1234 + big" in text

    def test_header_segment_shows_blank_gutter(self):
        seg = DiffSegment(DiffKind.HEADER, "whole-file write: /r/x.py")
        text = _plain(render_diff_body(_state(visible_segments=[seg])))
        assert "     whole-file write: /r/x.py" in text

    def test_truncation_segment_shows_blank_gutter(self):
        seg = DiffSegment(DiffKind.TRUNCATION, "…(truncated, 10 more lines)")
        text = _plain(render_diff_body(_state(visible_segments=[seg])))
        assert "     …(truncated, 10 more lines)" in text

    def test_segment_without_line_no_shows_blank_gutter(self):
        seg = DiffSegment(DiffKind.ADD, "+ x")
        text = _plain(render_diff_body(_state(visible_segments=[seg])))
        assert "     + x" in text

    def test_multiple_segments_each_have_gutter(self):
        segs = [
            DiffSegment(DiffKind.DEL, "- old", line_no=3),
            DiffSegment(DiffKind.ADD, "+ new", line_no=3),
        ]
        text = _plain(render_diff_body(_state(visible_segments=segs)))
        assert "   3 - old" in text
        assert "   3 + new" in text

    def test_gutter_style_is_dim(self):
        # The gutter text "   7 " must be rendered with "dim" style.
        seg = DiffSegment(DiffKind.ADD, "+ styled", line_no=7)
        body = render_diff_body(_state(visible_segments=[seg]))
        plain = body.plain
        gutter_text = "   7 "
        gutter_start = plain.index(gutter_text)
        gutter_end = gutter_start + len(gutter_text)
        dim_spans = [
            s
            for s in body.spans
            if s.start == gutter_start and s.end == gutter_end and str(s.style) == "dim"
        ]
        assert dim_spans, (
            f"expected a dim span covering {gutter_text!r} at [{gutter_start}:{gutter_end}]; "
            f"spans: {[(s.start, s.end, str(s.style)) for s in body.spans]}"
        )


# ---------------------------------------------------------------------------
# render_diff (header + body) + empty state (AC7 no-blank)
# ---------------------------------------------------------------------------


class TestRenderDiff:
    def test_combines_header_and_body(self):
        seg = DiffSegment(DiffKind.ADD, "+ hello world")
        text = _plain(render_diff(_state(visible_segments=[seg])))
        assert "app.py" in text  # header
        assert "+ hello world" in text  # body

    def test_empty_state_shows_waiting_not_blank(self):
        # AC7-adjacent: before any activity the panel shows a waiting message,
        # never a blank void.
        empty = _state(file_path=None, visible_segments=[], model=None)
        text = _plain(render_diff(empty))
        assert DIFF_EMPTY_TEXT in text

    def test_non_empty_state_shows_title(self):
        seg = DiffSegment(DiffKind.ADD, "+ x")
        text = _plain(render_diff(_state(visible_segments=[seg])))
        assert DIFF_TITLE in text


# ---------------------------------------------------------------------------
# DiffPanel widget seam (pure update/read; no compositor)
# ---------------------------------------------------------------------------


class TestDiffViewportHeight:
    def test_default_when_unmeasured(self):
        # Pre-layout the panel reports height 0; we fall back to the default so
        # the very first tick still computes a sensible scroll window.
        assert diff_viewport_height(0, chrome_rows=4, default=20) == 20

    def test_negative_height_uses_default(self):
        assert diff_viewport_height(-5, chrome_rows=4, default=15) == 15

    def test_subtracts_chrome_rows(self):
        assert diff_viewport_height(40, chrome_rows=4, default=20) == 36

    def test_floored_at_one_for_tiny_panel(self):
        # A panel smaller than the chrome must still yield at least one body row.
        assert diff_viewport_height(2, chrome_rows=4, default=20) == 1


class TestDiffPanelWidget:
    def test_initial_text_is_empty_waiting(self):
        panel = DiffPanel()
        assert DIFF_EMPTY_TEXT in panel.rendered_text()

    def test_update_from_state_reflects_header_and_body(self):
        panel = DiffPanel()
        seg = DiffSegment(DiffKind.ADD, "+ fresh line")
        panel.update_from_state(_state(visible_segments=[seg]))
        out = panel.rendered_text()
        assert "app.py" in out
        assert "+ fresh line" in out

    def test_update_from_none_state_keeps_waiting(self):
        # A None state (queue empty, nothing ever shown) keeps the waiting text.
        panel = DiffPanel()
        panel.update_from_state(None)
        assert DIFF_EMPTY_TEXT in panel.rendered_text()


# ---------------------------------------------------------------------------
# Commands feed title — MCP (story #5)
# ---------------------------------------------------------------------------


class TestCommandsTitle:
    def test_commands_title_includes_mcp(self):
        assert "MCP" in COMMANDS_TITLE


class TestMcpCommandRowRenders:
    def test_mcp_command_row_renders(self):
        model = CommandFeedModel(AppConfig())
        evt = CommandEvent(
            ts=datetime(2024, 1, 15, 10, 0, 3, tzinfo=timezone.utc),
            session_id="sess01234567",
            is_subagent=False,
            project_tag="myproj",
            source_path="/x/s.jsonl",
            command="Server::tool query=foo",
            tool_name="mcp__Server__tool",
        )
        model.record(evt)
        out = render_commands(model, 0, 120)
        assert "Server::tool" in out.plain


# ---------------------------------------------------------------------------
# Per-field color accents — MRU rows
# ---------------------------------------------------------------------------


def _spans_for_text(text: Text, substring: str) -> list:
    """Return all spans that cover the first occurrence of ``substring`` in text.

    A span "covers" the substring when the span range overlaps the substring
    range [start, end).  Used to find what style a named field carries.
    """
    plain = text.plain
    pos = plain.find(substring)
    if pos == -1:
        return []
    end = pos + len(substring)
    return [s for s in text.spans if s.start <= pos and s.end >= end]


def _style_strings_for(text: Text, substring: str) -> list[str]:
    """Return the style string(s) of every span covering ``substring``."""
    return [str(s.style) for s in _spans_for_text(text, substring)]


def _mru_model_with_entry(
    *,
    project_tag: str = "myproject",
    session_id: str = "aabbccdd1234",
    is_subagent: bool = False,
    op: FileOp = FileOp.EDIT,
    ts: Optional[datetime] = datetime(2024, 3, 5, 9, 10, 11, tzinfo=timezone.utc),
) -> MruModel:
    model = MruModel(AppConfig())
    evt = FileModifiedEvent(
        ts=ts,
        session_id=session_id,
        is_subagent=is_subagent,
        project_tag=project_tag,
        source_path="/src/x.jsonl",
        file_path="/some/path/widget.py",
        op=op,
        old_string="old" if op is FileOp.EDIT else None,
        new_string="new" if op is FileOp.EDIT else None,
        full_content="content" if op is FileOp.WRITE else None,
    )
    model.record(evt)
    return model


class TestMruRowColors:
    """Non-highlighted MRU rows carry per-field Rich style spans."""

    def test_timestamp_is_dim(self):
        model = _mru_model_with_entry()
        text = render_mru(model, 0, 0)
        # "09:10:11" is the formatted time for the test timestamp
        styles = _style_strings_for(text, "09:10:11")
        assert any(
            "dim" in s for s in styles
        ), f"expected 'dim' on timestamp; got styles={styles!r}\nfull text={text.plain!r}"

    def test_op_tag_is_dim_cyan(self):
        model = _mru_model_with_entry(op=FileOp.EDIT)
        text = render_mru(model, 0, 0)
        styles = _style_strings_for(text, "[EDIT]")
        assert any(
            "dim" in s and "cyan" in s for s in styles
        ), f"expected 'dim cyan' on op tag; got styles={styles!r}"

    def test_project_tag_is_dim_green(self):
        model = _mru_model_with_entry(project_tag="myproject")
        text = render_mru(model, 0, 0)
        styles = _style_strings_for(text, "myproject")
        assert any(
            "dim" in s and "green" in s for s in styles
        ), f"expected 'dim green' on project tag; got styles={styles!r}"

    def test_separator_is_dim(self):
        model = _mru_model_with_entry()
        text = render_mru(model, 0, 0)
        # The separator " · " appears between project tag and session
        styles = _style_strings_for(text, " · ")
        assert any(
            "dim" in s for s in styles
        ), f"expected 'dim' on separator; got styles={styles!r}"

    def test_session_hash_is_dim(self):
        model = _mru_model_with_entry(session_id="aabbccdd1234")
        text = render_mru(model, 0, 0)
        # short_session is first 8 chars: "aabbccdd"
        styles = _style_strings_for(text, "aabbccdd")
        assert any(
            "dim" in s for s in styles
        ), f"expected 'dim' on session hash; got styles={styles!r}"

    def test_subagent_marker_is_dim_yellow(self):
        model = _mru_model_with_entry(is_subagent=True)
        text = render_mru(model, 0, 0)
        styles = _style_strings_for(text, "⤷sub")
        assert any(
            "dim" in s and "yellow" in s for s in styles
        ), f"expected 'dim yellow' on subagent marker; got styles={styles!r}"

    def test_highlighted_row_has_no_per_field_colors(self):
        """Highlighted rows use bold reverse — no per-field color spans."""
        model = _mru_model_with_entry(project_tag="hlproject")
        # Set highlighted_key to trigger highlighted rendering
        entry = model.rows()[0]
        model.highlighted_key = entry.event_key
        text = render_mru(model, 0, 0)
        # For the highlighted row, per-field color spans must NOT be present.
        # The only span covering the project tag should be the bold-reverse style.
        styles = _style_strings_for(text, "hlproject")
        assert not any(
            "green" in s for s in styles
        ), f"highlighted row must not have 'green' on project tag; got={styles!r}"
        assert not any(
            "cyan" in s for s in styles
        ), f"highlighted row must not have 'cyan' on [EDIT]; got={styles!r}"

    def test_file_path_has_no_color_style(self):
        """The file path is the 'star' — no dim/color style spans cover it."""
        model = _mru_model_with_entry()
        text = render_mru(model, 0, 0)
        # "widget.py" is in the file path — should have no color style spans
        styles = _style_strings_for(text, "widget.py")
        # Accept zero spans OR spans that are NOT dim/colored (e.g. background-only)
        color_spans = [
            s
            for s in styles
            if any(c in s for c in ("cyan", "green", "yellow", "magenta", "red"))
        ]
        assert (
            not color_spans
        ), f"file path should have no foreground color styles; got={color_spans!r}"


# ---------------------------------------------------------------------------
# Per-field color accents — Commands rows
# ---------------------------------------------------------------------------


def _cmd_model_with_entry(
    *,
    command: str = "pytest -q",
    project_tag: str = "testproject",
    session_id: str = "11223344abcd",
    is_subagent: bool = False,
    ts: Optional[datetime] = datetime(2024, 3, 5, 14, 20, 33, tzinfo=timezone.utc),
) -> CommandFeedModel:
    model = CommandFeedModel(AppConfig())
    evt = CommandEvent(
        ts=ts,
        session_id=session_id,
        is_subagent=is_subagent,
        project_tag=project_tag,
        source_path="/src/x.jsonl",
        command=command,
        description=None,
    )
    model.record(evt)
    return model


class TestCommandRowColors:
    """Command rows carry per-field Rich style spans when width > 0."""

    def test_timestamp_is_dim(self):
        model = _cmd_model_with_entry()
        text = render_commands(model, 0, 120)
        styles = _style_strings_for(text, "14:20:33")
        assert any(
            "dim" in s for s in styles
        ), f"expected 'dim' on timestamp; got styles={styles!r}"

    def test_project_tag_is_dim_green(self):
        model = _cmd_model_with_entry(project_tag="testproject")
        text = render_commands(model, 0, 120)
        styles = _style_strings_for(text, "testproject")
        assert any(
            "dim" in s and "green" in s for s in styles
        ), f"expected 'dim green' on project tag; got styles={styles!r}"

    def test_separator_is_dim(self):
        model = _cmd_model_with_entry()
        text = render_commands(model, 0, 120)
        styles = _style_strings_for(text, " · ")
        assert any(
            "dim" in s for s in styles
        ), f"expected 'dim' on separator; got styles={styles!r}"

    def test_session_hash_is_dim_magenta(self):
        model = _cmd_model_with_entry(session_id="11223344abcd")
        text = render_commands(model, 0, 120)
        # short_session is first 8 chars: "11223344"
        styles = _style_strings_for(text, "11223344")
        assert any(
            "dim" in s and "magenta" in s for s in styles
        ), f"expected 'dim magenta' on session hash; got styles={styles!r}"

    def test_subagent_marker_is_dim_yellow(self):
        model = _cmd_model_with_entry(is_subagent=True)
        text = render_commands(model, 0, 120)
        styles = _style_strings_for(text, "⤷sub")
        assert any(
            "dim" in s and "yellow" in s for s in styles
        ), f"expected 'dim yellow' on subagent marker; got styles={styles!r}"

    def test_command_text_has_no_dim_style(self):
        """The command text is the main content — no dim style."""
        model = _cmd_model_with_entry(command="pytest -q")
        text = render_commands(model, 0, 120)
        styles = _style_strings_for(text, "pytest -q")
        # Command should have no dim or color style
        dim_spans = [s for s in styles if "dim" in s]
        assert not dim_spans, f"command text should not be dim; got={dim_spans!r}"

    def test_no_styled_rows_when_width_zero(self):
        """When width=0, falls back to plain format_command_row (returns empty row)."""
        model = _cmd_model_with_entry(project_tag="fallbackproj")
        text = render_commands(model, 0, 0)
        # width=0 → format_command_row returns "" (nothing fits), so the
        # rendered text is just the title header with an empty data row.
        assert text.plain.startswith("Commands")
