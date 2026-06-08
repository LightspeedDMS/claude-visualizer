"""Tests for the Textual UI — pure panel formatters + the REAL app harness.

Two layers:

1. ``ui.panels`` pure formatters (no Textual runtime, no IO): row rendering and
   the MRU block, including the ``project · <short session>`` origin tag and the
   ``⤷sub`` subagent marker.  Fast, deterministic, anti-mock (real MruModel).

2. ``ui.app.VisualizerApp`` driven through Textual's REAL test harness
   (``async with app.run_test() as pilot``).  This runs the ACTUAL application
   event loop and the ACTUAL pipeline against a REAL temporary ``projects_root``
   — no mocks.  We append real JSONL lines to real transcript files and assert
   the live MRU panel reflects them after ``await pilot.pause()``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from claude_visualizer.config import AppConfig
from claude_visualizer.events import CommandEvent, FileOp
from claude_visualizer.models.command_feed import CommandFeedEntry, CommandFeedModel
from claude_visualizer.models.mru import MruEntry, MruModel
from claude_visualizer.ui.app import VisualizerApp
from claude_visualizer.ui.panels import (
    COMMANDS_EMPTY_TEXT,
    MISSING_TIME_TEXT,
    MRU_EMPTY_TEXT,
    MRU_HIGHLIGHT_MARKER,
    MRU_ROW_STYLE_ODD,
    SUBAGENT_MARKER,
    THINKING_GLYPH,
    TRUNCATION_ELLIPSIS,
    CommandsPanel,
    DiffPanel,
    MonitorBar,
    MruFilesPanel,
    format_command_row,
    format_mru_row,
    render_commands,
    render_monitor_bar,
    render_mru,
    truncate_command,
)


class _ManualClock:
    """A test-controlled monotonic clock injected into the app's diff queue.

    The live app advances the diff queue in real time; tests instead drive this
    clock explicitly so dwell/scroll/advance are deterministic with no sleeps.
    """

    def __init__(self, start: float = 0.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _entry(
    file_path: str = "/repo/main.py",
    project_tag: str = "my-project",
    short_session: str = "abc12345",
    is_subagent: bool = False,
    op: FileOp = FileOp.WRITE,
    ts: datetime | None = datetime(2024, 1, 2, 13, 45, 7, tzinfo=timezone.utc),
) -> MruEntry:
    return MruEntry(
        file_path=file_path,
        project_tag=project_tag,
        short_session=short_session,
        is_subagent=is_subagent,
        op=op,
        ts=ts,
    )


def _write_tool_line(
    name: str,
    inp: dict,
    *,
    session_id: str = "sessAAAA1111",
    cwd: str = "/home/dev/my-project",
    model: str = "claude-opus-4-5",
) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "timestamp": "2024-01-15T10:00:00.000Z",
            "sessionId": session_id,
            "cwd": cwd,
            "message": {
                "role": "assistant",
                "model": model,
                "content": [
                    {"type": "tool_use", "id": "t1", "name": name, "input": inp}
                ],
            },
        }
    )


def _append_line(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
        fh.flush()


def _fixture_config(root: Path, **overrides) -> AppConfig:
    # Default monitors_dir to an empty controlled directory so VisualizerApp
    # boots load ZERO real monitors (no proxmox_cluster.py, no zzz_machine_stats.py).
    # Callers may override via monitors_dir=<path> in overrides.
    monitors_empty = root.parent / "monitors_empty"
    monitors_empty.mkdir(parents=True, exist_ok=True)
    base = dict(
        projects_root=root,
        active_window_seconds=3600,
        discovery_interval_seconds=0.05,
        poll_interval_seconds=0.05,
        seed_tail_bytes=65_536,
        max_line_bytes=1_000_000,
        mru_max=50,
        cache_path=None,  # tests must not touch ~/.claude-visualizer/cache.db
        monitors_dir=monitors_empty,  # tests must not touch ~/.claude-visualizer/monitors/
    )
    base.update(overrides)
    return AppConfig(**base)


async def _pump(pilot, panel_text_contains: str, *, tries: int = 40) -> str:
    """Pause repeatedly until the MRU panel text contains the marker.

    Returns the final rendered panel text.  Bounded loop (anti-unbounded):
    at most ``tries`` pauses, then we return whatever is there for the
    assertion to report a clear failure.
    """
    panel = pilot.app.query_one(MruFilesPanel)
    text = ""
    for _ in range(tries):
        await pilot.pause()
        text = panel.rendered_text()
        if panel_text_contains in text:
            return text
    return text


async def _pump_commands(pilot, contains: str, *, tries: int = 40) -> str:
    """Pause repeatedly until the Commands panel text contains ``contains``.

    The bottom Commands feed is repainted both on the consume loop (when a
    command is routed) and on the periodic tick, so a plain ``pilot.pause()``
    between checks is enough — no clock nudging needed.  Bounded loop
    (anti-unbounded): at most ``tries`` pauses, then return whatever is there so
    a failing assertion reports the actual rendered text.
    """
    panel = pilot.app.query_one(CommandsPanel)
    text = ""
    for _ in range(tries):
        await pilot.pause()
        text = panel.rendered_text()
        if contains in text:
            return text
    return text


async def _pump_diff(
    pilot, contains: str, *, clock: _ManualClock, tries: int = 60
) -> str:
    """Pump the real app until the Diff panel text contains ``contains``.

    The diff queue advances on the app's periodic tick; the app reads time from
    the injected ``clock``, so we nudge the clock a little between pauses to let
    the queue promote a freshly-recorded file (it only becomes displayable on a
    LATER tick).  Bounded (anti-unbounded): at most ``tries`` iterations.
    """
    panel = pilot.app.query_one(DiffPanel)
    text = ""
    for _ in range(tries):
        clock.advance(0.05)
        await pilot.pause()
        text = panel.rendered_text()
        if contains in text:
            return text
    return text


def _thinking_lines(
    *,
    file_path: str,
    session_id: str,
    cwd: str,
    request_id: str = "req_think_LIVE",
    model: str = "claude-opus-4-8",
) -> list[str]:
    """Two JSONL lines: a thinking block then a Write tool_use sharing requestId.

    Mirrors the real transcript shape — the ``{type:"thinking"}`` entry PRECEDES
    the ``tool_use`` entry and both carry the same entry-level ``requestId`` — so
    the engine's correlator flags ``used_thinking`` on the resulting event (AC4).
    """
    think = json.dumps(
        {
            "type": "assistant",
            "timestamp": "2024-02-01T09:00:01.000Z",
            "sessionId": session_id,
            "cwd": cwd,
            "requestId": request_id,
            "message": {
                "role": "assistant",
                "model": model,
                "content": [
                    {
                        "type": "thinking",
                        "thinking": "Reasoning before the write.",
                        "signature": "sig-live",
                    }
                ],
            },
        }
    )
    write = json.dumps(
        {
            "type": "assistant",
            "timestamp": "2024-02-01T09:00:02.000Z",
            "sessionId": session_id,
            "cwd": cwd,
            "requestId": request_id,
            "message": {
                "role": "assistant",
                "model": model,
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_live",
                        "name": "Write",
                        "input": {"file_path": file_path, "content": "x=1"},
                    }
                ],
            },
        }
    )
    return [think, write]


# ---------------------------------------------------------------------------
# Pure formatters (ui.panels)
# ---------------------------------------------------------------------------


class TestFormatMruRow:
    def test_row_contains_file_path(self):
        line = format_mru_row(_entry(file_path="/repo/widget.py"))
        assert "/repo/widget.py" in line

    def test_row_contains_project_and_session_origin(self):
        line = format_mru_row(_entry(project_tag="cool-proj", short_session="deadbeef"))
        # Origin rendered as "project · session".
        assert "cool-proj" in line
        assert "deadbeef" in line
        assert "·" in line

    def test_non_subagent_row_has_no_marker(self):
        line = format_mru_row(_entry(is_subagent=False))
        assert SUBAGENT_MARKER not in line

    def test_subagent_row_has_marker(self):
        line = format_mru_row(_entry(is_subagent=True))
        assert SUBAGENT_MARKER in line

    def test_row_contains_time(self):
        # The per-row timestamp is rendered HH:MM:SS, consistent with the feed.
        line = format_mru_row(
            _entry(ts=datetime(2024, 1, 2, 13, 45, 7, tzinfo=timezone.utc))
        )
        assert "13:45:07" in line

    def test_time_is_leftmost_column(self):
        # NEW layout: the HH:MM:SS time is the FIRST thing on the line, before
        # the [OP] marker and the path (timestamp leftmost column).
        line = format_mru_row(
            _entry(
                file_path="/repo/widget.py",
                op=FileOp.EDIT,
                ts=datetime(2024, 1, 2, 13, 45, 7, tzinfo=timezone.utc),
            )
        )
        assert line.startswith("13:45:07")
        # Time precedes the op tag, which precedes the path.
        assert line.index("13:45:07") < line.index("[EDIT]")
        assert line.index("[EDIT]") < line.index("/repo/widget.py")

    def test_highlighted_time_is_leftmost_before_marker(self):
        # Even on the highlighted row the time leads — the ▶ marker comes AFTER
        # the time (time is the leftmost column on every row).
        line = format_mru_row(
            _entry(
                file_path="/repo/hot.py",
                ts=datetime(2024, 1, 2, 13, 45, 7, tzinfo=timezone.utc),
            ),
            highlighted=True,
        )
        assert line.startswith("13:45:07")
        assert MRU_HIGHLIGHT_MARKER in line
        # The highlight marker and op/path all follow the time.
        assert line.index("13:45:07") < line.index(MRU_HIGHLIGHT_MARKER)
        assert line.index(MRU_HIGHLIGHT_MARKER) < line.index("/repo/hot.py")

    def test_missing_timestamp_renders_placeholder(self):
        # A None timestamp must not raise and must show the stable placeholder.
        line = format_mru_row(_entry(file_path="/repo/x.py", ts=None))
        assert "/repo/x.py" in line
        assert MISSING_TIME_TEXT in line
        # The placeholder still occupies the leftmost column.
        assert line.startswith(MISSING_TIME_TEXT)


class TestRenderMru:
    def test_empty_model_renders_placeholder(self):
        model = MruModel(AppConfig(mru_max=10))
        text = render_mru(model)
        assert MRU_EMPTY_TEXT in text

    def test_rows_rendered_newest_first(self):
        model = MruModel(AppConfig(mru_max=10))
        # record a, then b → b should appear before a (newest-first).
        for path, sess in (("/r/a.py", "aaaa0000"), ("/r/b.py", "bbbb1111")):
            model.record(_file_event(file_path=path, session_id=sess))
        # render_mru now returns a Rich Text (colours the highlighted row);
        # assert ordering against its plain string.
        text = render_mru(model).plain
        assert text.index("/r/b.py") < text.index("/r/a.py")

    def test_subagent_marker_present_for_subagent_row(self):
        model = MruModel(AppConfig(mru_max=10))
        model.record(_file_event(file_path="/r/sub.py", is_subagent=True))
        assert SUBAGENT_MARKER in render_mru(model)

    def test_rows_contain_time(self):
        # The rendered MRU block shows each row's HH:MM:SS timestamp.
        model = MruModel(AppConfig(mru_max=10))
        model.record(
            _file_event(
                file_path="/r/timed.py",
                ts=datetime(2024, 5, 6, 7, 8, 9, tzinfo=timezone.utc),
            )
        )
        text = render_mru(model).plain
        assert "/r/timed.py" in text
        assert "07:08:09" in text

    def test_odd_rows_carry_zebra_style_span(self):
        # Two entries: rows()[0]=b (newest, index 0=even), rows()[1]=a (index 1=odd).
        # The odd row must have exactly one Rich span using MRU_ROW_STYLE_ODD.
        model = MruModel(AppConfig(mru_max=10))
        model.record(_file_event(file_path="/r/a.py"))
        model.record(_file_event(file_path="/r/b.py"))
        text = render_mru(model)
        zebra = [s for s in text.spans if MRU_ROW_STYLE_ODD in str(s.style)]
        assert len(zebra) == 1

    def test_single_even_row_has_no_zebra_span(self):
        # A single entry sits at index 0 (even) — no alternating span expected.
        model = MruModel(AppConfig(mru_max=10))
        model.record(_file_event(file_path="/r/only.py"))
        text = render_mru(model)
        zebra = [s for s in text.spans if MRU_ROW_STYLE_ODD in str(s.style)]
        assert len(zebra) == 0

    def test_highlighted_odd_row_uses_highlight_not_zebra(self):
        # When an odd row is highlighted, MRU_HIGHLIGHT_STYLE replaces the zebra
        # tint: no zebra span present, and the highlight span IS present.
        from claude_visualizer.ui.panels import MRU_HIGHLIGHT_STYLE

        model = MruModel(AppConfig(mru_max=10))
        model.record(_file_event(file_path="/r/a.py"))  # row 1 (odd)
        model.record(_file_event(file_path="/r/b.py"))  # row 0 (even, newest)
        model.highlighted_path = "/r/a.py"
        text = render_mru(model)
        zebra = [s for s in text.spans if MRU_ROW_STYLE_ODD in str(s.style)]
        highlight = [s for s in text.spans if MRU_HIGHLIGHT_STYLE in str(s.style)]
        assert len(zebra) == 0
        assert len(highlight) == 1

    def test_short_path_padded_to_width(self):
        """A path shorter than panel width is padded to exactly width."""
        model = MruModel(AppConfig(mru_max=10))
        model.record(_file_event(file_path="a.py"))
        # width=80: the decorated row ("HH:MM:SS ... a.py   proj · sess1234") is ~42 chars,
        # shorter than 80, so it pads to exactly 80.
        text = render_mru(model, width=80)
        plain = text.plain
        # plain starts with the title + blank line; the actual row is the last segment
        row_content = plain.split("\n")[-1]
        assert len(row_content) == 80, f"Expected 80, got {len(row_content)}"

    def test_long_path_padded_to_next_width_multiple(self):
        """A path longer than panel width gets padded to the next multiple of
        width so every wrapped visual line has a full-width background span."""
        model = MruModel(AppConfig(mru_max=10))
        # Record two entries so the second (odd) row gets a zebra span — the
        # long path is the FIRST recorded (oldest) so it ends up at index 1 (odd).
        model.record(
            _file_event(
                file_path="/very/long/path/that/exceeds/panel/width/filename.py"
            )
        )
        model.record(_file_event(file_path="/r/b.py"))  # newest → index 0 (even)
        # Use width=20 so the long path row (index 1 = odd) exceeds 1 visual line.
        text = render_mru(model, width=20)
        plain = text.plain
        # Split on newline; the two rows are separated by a single '\n'.
        # rows()[0] = newest = b.py (even, no zebra), rows()[1] = long path (odd, zebra).
        lines = plain.split("\n")
        # plain = "title\n\nnewst_row\nlong_row"; long path is oldest → last line
        long_row = lines[-1]
        assert (
            len(long_row) % 20 == 0
        ), f"Expected len to be multiple of 20, got {len(long_row)}"
        # The zebra-odd span must cover the padded length.
        zebra = [s for s in text.spans if MRU_ROW_STYLE_ODD in str(s.style)]
        assert len(zebra) >= 1
        span = zebra[0]
        assert (span.end - span.start) == len(
            long_row
        ), f"Span length {span.end - span.start} != row length {len(long_row)}"


# ---------------------------------------------------------------------------
# Commands feed pure formatters (ui.panels) — story #4
# ---------------------------------------------------------------------------


def _cmd_entry(
    command: str = "ls -la",
    project_tag: str = "my-project",
    short_session: str = "abc12345",
    is_subagent: bool = False,
    ts: datetime | None = datetime(2024, 1, 2, 13, 45, 7, tzinfo=timezone.utc),
) -> CommandFeedEntry:
    return CommandFeedEntry(
        command=command,
        ts=ts,
        project_tag=project_tag,
        short_session=short_session,
        is_subagent=is_subagent,
    )


def _cmd_event(
    command: str = "ls -la",
    session_id: str = "abc12345xyz",
    project_tag: str = "proj",
    is_subagent: bool = False,
) -> CommandEvent:
    return CommandEvent(
        ts=datetime(2024, 1, 2, 13, 45, 7, tzinfo=timezone.utc),
        session_id=session_id,
        is_subagent=is_subagent,
        project_tag=project_tag,
        source_path="/x/a.jsonl",
        command=command,
    )


class TestTruncateCommand:
    def test_short_command_unchanged(self):
        assert truncate_command("ls", 40) == "ls"

    def test_command_at_exact_width_unchanged(self):
        text = "x" * 10
        assert truncate_command(text, 10) == text

    def test_long_command_truncated_with_ellipsis(self):
        out = truncate_command("x" * 100, 10)
        assert out.endswith(TRUNCATION_ELLIPSIS)
        # Total visible width never exceeds the requested width.
        assert len(out) == 10

    def test_truncation_keeps_leading_command_text(self):
        out = truncate_command("supercalifragilistic", 8)
        assert out.startswith("superc")  # first 7 chars + ellipsis
        assert out.endswith(TRUNCATION_ELLIPSIS)
        assert len(out) == 8

    def test_newlines_collapsed_to_single_line(self):
        # A multi-line command must render on ONE feed row (no embedded \n).
        out = truncate_command("echo a\necho b", 40)
        assert "\n" not in out

    def test_width_one_yields_just_ellipsis(self):
        # Degenerate but bounded: width 1 → only the ellipsis fits.
        out = truncate_command("anything", 1)
        assert out == TRUNCATION_ELLIPSIS

    def test_non_positive_width_yields_empty(self):
        assert truncate_command("anything", 0) == ""


class TestFormatCommandRow:
    def test_row_contains_command(self):
        line = format_command_row(_cmd_entry(command="pytest -q"), width=80)
        assert "pytest -q" in line

    def test_row_contains_time(self):
        line = format_command_row(_cmd_entry(), width=80)
        # Time rendered as HH:MM:SS from the entry timestamp.
        assert "13:45:07" in line

    def test_time_is_leftmost_column(self):
        # NEW layout: the HH:MM:SS time is the FIRST thing on the row, before
        # the command (timestamp leftmost column).
        line = format_command_row(
            _cmd_entry(
                command="pytest -q",
                ts=datetime(2024, 1, 2, 13, 45, 7, tzinfo=timezone.utc),
            ),
            width=80,
        )
        assert line.startswith("13:45:07")
        # Time precedes the command, which precedes the origin.
        assert line.index("13:45:07") < line.index("pytest -q")
        assert line.index("pytest -q") < line.index("my-project")

    def test_row_contains_project_and_session_origin(self):
        line = format_command_row(
            _cmd_entry(project_tag="cool-proj", short_session="deadbeef"),
            width=80,
        )
        assert "cool-proj" in line
        assert "deadbeef" in line
        assert "·" in line

    def test_non_subagent_row_has_no_marker(self):
        line = format_command_row(_cmd_entry(is_subagent=False), width=80)
        assert SUBAGENT_MARKER not in line

    def test_subagent_row_has_marker(self):
        line = format_command_row(_cmd_entry(is_subagent=True), width=80)
        assert SUBAGENT_MARKER in line

    def test_long_command_truncated_to_fit_row_width(self):
        line = format_command_row(_cmd_entry(command="y" * 500), width=60)
        assert TRUNCATION_ELLIPSIS in line
        # The WHOLE row fits within the panel width (command field shrinks).
        assert len(line) <= 60
        # And it stays on one line.
        assert "\n" not in line

    def test_missing_timestamp_renders_placeholder(self):
        line = format_command_row(_cmd_entry(ts=None), width=80)
        # Row still renders (no exception), shows the command, and a stable
        # time placeholder instead of a real clock.
        assert "ls -la" in line
        assert MISSING_TIME_TEXT in line


class TestRenderCommands:
    def test_empty_model_renders_placeholder(self):
        model = CommandFeedModel(AppConfig(command_feed_max=10))
        text = render_commands(model, width=80).plain
        assert COMMANDS_EMPTY_TEXT in text

    def test_commands_rendered_newest_on_top(self):
        model = CommandFeedModel(AppConfig(command_feed_max=10))
        model.record(_cmd_event(command="older-cmd"))
        model.record(_cmd_event(command="newer-cmd"))
        text = render_commands(model, width=80).plain
        assert text.index("newer-cmd") < text.index("older-cmd")

    def test_no_dedup_both_identical_rows_rendered(self):
        model = CommandFeedModel(AppConfig(command_feed_max=10))
        model.record(_cmd_event(command="dup-cmd"))
        model.record(_cmd_event(command="dup-cmd"))
        text = render_commands(model, width=80).plain
        assert text.count("dup-cmd") == 2

    def test_subagent_marker_present_for_subagent_row(self):
        model = CommandFeedModel(AppConfig(command_feed_max=10))
        model.record(_cmd_event(command="sub-cmd", is_subagent=True))
        assert SUBAGENT_MARKER in render_commands(model, width=80).plain


class TestCommandsPanelWidget:
    def test_panel_starts_with_empty_text(self):
        panel = CommandsPanel()
        assert COMMANDS_EMPTY_TEXT in panel.rendered_text()

    def test_update_from_model_reflects_recorded_command(self):
        panel = CommandsPanel()
        model = CommandFeedModel(AppConfig(command_feed_max=10))
        model.record(_cmd_event(command="make build"))
        panel.update_from_model(model, width=80)
        assert "make build" in panel.rendered_text()


def _file_event(
    file_path: str = "/r/x.py",
    session_id: str = "sess1234abcd",
    is_subagent: bool = False,
    project_tag: str = "proj",
    ts: datetime | None = datetime(2024, 1, 2, 13, 45, 7, tzinfo=timezone.utc),
):
    from claude_visualizer.events import FileModifiedEvent

    return FileModifiedEvent(
        ts=ts,
        session_id=session_id,
        is_subagent=is_subagent,
        project_tag=project_tag,
        source_path="/x/a.jsonl",
        file_path=file_path,
        op=FileOp.WRITE,
    )


# ---------------------------------------------------------------------------
# REAL Textual app harness (ui.app.VisualizerApp)
# ---------------------------------------------------------------------------


class TestVisualizerAppLayout:
    async def test_three_regions_exist(self, tmp_path: Path):
        cfg = _fixture_config(tmp_path / "projects")
        app = VisualizerApp(cfg)
        async with app.run_test() as pilot:
            # Top-left: the active MRU panel.
            assert pilot.app.query_one("#mru-panel") is not None
            # Top-right + bottom: labeled placeholders for future stories.
            assert pilot.app.query_one("#top-right") is not None
            assert pilot.app.query_one("#bottom") is not None

    async def test_top_right_is_diff_panel_bottom_is_commands_panel(
        self, tmp_path: Path
    ):
        cfg = _fixture_config(tmp_path / "projects")
        app = VisualizerApp(cfg)
        async with app.run_test() as pilot:
            # Top-right is the live Diff panel (story #3 realized).
            tr = pilot.app.query_one("#top-right")
            assert isinstance(tr, DiffPanel)
            # Bottom is now the live Commands feed panel (story #4 realized).
            bottom = pilot.app.query_one("#bottom")
            assert isinstance(bottom, CommandsPanel)
            # Before any command it shows its waiting message.
            assert COMMANDS_EMPTY_TEXT in bottom.rendered_text()


class TestVisualizerAppLiveMru:
    async def test_appended_write_appears_in_mru_panel(self, tmp_path: Path):
        root = tmp_path / "projects"
        session = root / "proj" / "session-live.jsonl"
        _append_line(
            session,
            _write_tool_line("Write", {"file_path": "/r/seed.py", "content": "x"}),
        )
        cfg = _fixture_config(root)
        app = VisualizerApp(cfg)
        async with app.run_test() as pilot:
            _append_line(
                session,
                _write_tool_line(
                    "Edit",
                    {
                        "file_path": "/r/live_edit.py",
                        "old_string": "a",
                        "new_string": "b",
                    },
                    session_id="liveSESS9999",
                    cwd="/home/dev/widgets",
                ),
            )
            text = await _pump(pilot, "/r/live_edit.py")
            assert "/r/live_edit.py" in text
            # Origin tag: project (basename cwd) + short session.
            assert "widgets" in text
            assert "liveSESS" in text

    async def test_subagent_file_shows_marker_in_panel(self, tmp_path: Path):
        root = tmp_path / "projects"
        sub = root / "proj" / "sess" / "subagents" / "agent-xyz.jsonl"
        cfg = _fixture_config(root)
        app = VisualizerApp(cfg)
        async with app.run_test() as pilot:
            _append_line(
                sub,
                _write_tool_line(
                    "Write",
                    {"file_path": "/r/from_sub.py", "content": "s"},
                    session_id="subSESS0001",
                ),
            )
            text = await _pump(pilot, "/r/from_sub.py")
            assert "/r/from_sub.py" in text
            assert SUBAGENT_MARKER in text


class TestVisualizerAppResize:
    async def test_resize_does_not_crash_and_panel_persists(self, tmp_path: Path):
        root = tmp_path / "projects"
        session = root / "proj" / "s.jsonl"
        _append_line(
            session,
            _write_tool_line("Write", {"file_path": "/r/seed.py", "content": "x"}),
        )
        cfg = _fixture_config(root)
        app = VisualizerApp(cfg)
        async with app.run_test(size=(100, 40)) as pilot:
            _append_line(
                session,
                _write_tool_line(
                    "Write", {"file_path": "/r/before_resize.py", "content": "y"}
                ),
            )
            await _pump(pilot, "/r/before_resize.py")
            # Resize the terminal — Textual must re-lay-out the 3 regions.
            await pilot.resize_terminal(60, 20)
            await pilot.pause()
            panel = pilot.app.query_one(MruFilesPanel)
            # Panel still present and still showing the recorded file.
            assert "/r/before_resize.py" in panel.rendered_text()
            # All three regions survive the resize.
            assert pilot.app.query_one("#top-right") is not None
            assert pilot.app.query_one("#bottom") is not None


class TestVisualizerAppDiffPanel:
    """The REAL app renders the live Diff panel from the diff queue (story #3)."""

    async def test_edit_renders_colored_diff_and_header(self, tmp_path: Path):
        root = tmp_path / "projects"
        session = root / "proj" / "edit.jsonl"
        clock = _ManualClock()
        cfg = _fixture_config(root)
        app = VisualizerApp(cfg, now=clock)
        async with app.run_test(size=(120, 40)) as pilot:
            _append_line(
                session,
                _write_tool_line(
                    "Edit",
                    {
                        "file_path": "/repo/calc.py",
                        "old_string": "return a - b",
                        "new_string": "return a + b",
                    },
                    session_id="editSESS1234",
                    cwd="/home/dev/calculator",
                    model="claude-opus-4-8",
                ),
            )
            text = await _pump_diff(pilot, "calc.py", clock=clock)
            # Header: short model + filename + origin.
            assert "opus-4-8" in text
            assert "calc.py" in text
            assert "calculator" in text  # project (basename cwd)
            assert "editSESS" in text  # short session
            # Body: the unified diff transforms old → new (DEL then ADD).
            assert "- return a - b" in text
            assert "+ return a + b" in text
            # The diff body actually carries colour spans (green/red).
            panel = pilot.app.query_one(DiffPanel)
            styles = " ".join(str(s.style) for s in panel._renderable.spans)
            assert "#98c379" in styles
            assert "#e06c75" in styles

    async def test_write_renders_whole_file_additions_with_label(self, tmp_path: Path):
        root = tmp_path / "projects"
        session = root / "proj" / "write.jsonl"
        clock = _ManualClock()
        cfg = _fixture_config(root)
        app = VisualizerApp(cfg, now=clock)
        async with app.run_test(size=(120, 40)) as pilot:
            _append_line(
                session,
                _write_tool_line(
                    "Write",
                    {
                        "file_path": "/repo/brand_new.py",
                        "content": "import os\nprint(os.getcwd())",
                    },
                    session_id="writeSESS999",
                    cwd="/home/dev/fresh",
                ),
            )
            text = await _pump_diff(pilot, "brand_new.py", clock=clock)
            # AC2: labelled whole-file write, all-green additions, no DEL lines.
            assert "whole-file write" in text
            assert "+ import os" in text
            assert "+ print(os.getcwd())" in text
            assert "- " not in text  # no fabricated removals

    async def test_thinking_turn_shows_brain_glyph_in_header(self, tmp_path: Path):
        root = tmp_path / "projects"
        session = root / "proj" / "think.jsonl"
        clock = _ManualClock()
        cfg = _fixture_config(root)
        app = VisualizerApp(cfg, now=clock)
        async with app.run_test(size=(120, 40)) as pilot:
            for line in _thinking_lines(
                file_path="/repo/thoughtful.py",
                session_id="thinkSESS01",
                cwd="/home/dev/thinkproj",
            ):
                _append_line(session, line)
            text = await _pump_diff(pilot, "thoughtful.py", clock=clock)
            # AC3/AC4: the 🧠 glyph appears because thinking shared the requestId.
            assert THINKING_GLYPH in text
            assert "thoughtful.py" in text

    async def test_displayed_file_highlighted_in_mru(self, tmp_path: Path):
        root = tmp_path / "projects"
        session = root / "proj" / "sync.jsonl"
        clock = _ManualClock()
        cfg = _fixture_config(root)
        app = VisualizerApp(cfg, now=clock)
        async with app.run_test(size=(120, 40)) as pilot:
            _append_line(
                session,
                _write_tool_line(
                    "Write",
                    {"file_path": "/repo/highlighted.py", "content": "h=1"},
                    session_id="syncSESS001",
                    cwd="/home/dev/syncproj",
                ),
            )
            # Wait for the diff panel to show the file…
            await _pump_diff(pilot, "highlighted.py", clock=clock)
            # …then AC9: that same file is highlighted in the MRU list.
            mru = pilot.app.query_one(MruFilesPanel)
            mru_text = mru.rendered_text()
            assert "/repo/highlighted.py" in mru_text
            highlighted_rows = [
                ln
                for ln in mru_text.splitlines()
                if "/repo/highlighted.py" in ln and MRU_HIGHLIGHT_MARKER in ln
            ]
            assert highlighted_rows, (
                "displayed file must be highlighted in the MRU list (AC9); "
                f"MRU text was:\n{mru_text}"
            )


class TestVisualizerAppLiveCommands:
    """The REAL app renders the live bottom Commands feed (story #4)."""

    async def test_bash_commands_from_two_sessions_and_subagent_newest_on_top(
        self, tmp_path: Path
    ):
        root = tmp_path / "projects"
        sess1 = root / "proj" / "session-one.jsonl"
        sess2 = root / "proj" / "session-two.jsonl"
        sub = root / "proj" / "sess" / "subagents" / "agent-aaa.jsonl"
        cfg = _fixture_config(root)
        app = VisualizerApp(cfg)
        async with app.run_test(size=(120, 40)) as pilot:
            # Session one command…
            _append_line(
                sess1,
                _write_tool_line(
                    "Bash",
                    {"command": "echo from-session-one"},
                    session_id="sessONE11111",
                    cwd="/home/dev/alpha",
                ),
            )
            await _pump_commands(pilot, "echo from-session-one")
            # …a subagent command…
            _append_line(
                sub,
                _write_tool_line(
                    "Bash",
                    {"command": "echo from-subagent"},
                    session_id="subSESS22222",
                    cwd="/home/dev/alpha",
                ),
            )
            await _pump_commands(pilot, "echo from-subagent")
            # …and a second-session command last (so it must be on top).
            _append_line(
                sess2,
                _write_tool_line(
                    "Bash",
                    {"command": "echo from-session-two"},
                    session_id="sessTWO33333",
                    cwd="/home/dev/beta",
                ),
            )
            text = await _pump_commands(pilot, "echo from-session-two")

            # All three commands present.
            assert "echo from-session-one" in text
            assert "echo from-subagent" in text
            assert "echo from-session-two" in text
            # Newest-on-top: session-two (last appended) precedes the others.
            assert text.index("echo from-session-two") < text.index(
                "echo from-subagent"
            )
            assert text.index("echo from-subagent") < text.index(
                "echo from-session-one"
            )
            # Origin tags: project (basename cwd) + short session.
            assert "alpha" in text
            assert "beta" in text
            assert "sessTWO3" in text
            # The subagent row carries the ⤷sub marker.
            sub_rows = [
                ln
                for ln in text.splitlines()
                if "echo from-subagent" in ln and SUBAGENT_MARKER in ln
            ]
            assert sub_rows, (
                "subagent command must show the ⤷sub marker; "
                f"commands text was:\n{text}"
            )

    async def test_same_command_twice_shows_both_rows_no_dedup(self, tmp_path: Path):
        root = tmp_path / "projects"
        session = root / "proj" / "dup.jsonl"
        cfg = _fixture_config(root)
        app = VisualizerApp(cfg)
        async with app.run_test(size=(120, 40)) as pilot:
            for _ in range(2):
                _append_line(
                    session,
                    _write_tool_line(
                        "Bash",
                        {"command": "make deploy-prod"},
                        session_id="dupSESS00001",
                        cwd="/home/dev/dupproj",
                    ),
                )
            # Pump until at least one shows, then a couple more pauses so the
            # second identical line is surely consumed too.
            await _pump_commands(pilot, "make deploy-prod")
            for _ in range(8):
                await pilot.pause()
            panel = pilot.app.query_one(CommandsPanel)
            text = panel.rendered_text()
            # AC2: BOTH identical commands present as separate rows (no dedup).
            assert text.count("make deploy-prod") == 2

    async def test_long_command_truncated_in_feed(self, tmp_path: Path):
        root = tmp_path / "projects"
        session = root / "proj" / "long.jsonl"
        cfg = _fixture_config(root)
        app = VisualizerApp(cfg)
        async with app.run_test(size=(120, 40)) as pilot:
            long_cmd = "echo " + "z" * 400  # far wider than any panel
            _append_line(
                session,
                _write_tool_line(
                    "Bash",
                    {"command": long_cmd},
                    session_id="longSESS9999",
                    cwd="/home/dev/longproj",
                ),
            )
            # The leading recognizable prefix appears…
            text = await _pump_commands(pilot, "echo zzz")
            # …and the row is truncated with the … ellipsis (AC3).
            assert TRUNCATION_ELLIPSIS in text
            # The full 400-z payload must NOT be present (it was truncated).
            assert "z" * 400 not in text

    async def test_overflow_drops_oldest_command(self, tmp_path: Path):
        root = tmp_path / "projects"
        session = root / "proj" / "overflow.jsonl"
        cfg = _fixture_config(root, command_feed_max=3)
        app = VisualizerApp(cfg)
        async with app.run_test(size=(120, 40)) as pilot:
            for i in range(5):
                _append_line(
                    session,
                    _write_tool_line(
                        "Bash",
                        {"command": f"step-{i}-run"},
                        session_id="ovrSESS00001",
                        cwd="/home/dev/ovrproj",
                    ),
                )
            # Wait for the newest to surface, then drain a few more pauses so
            # every appended line is consumed.
            await _pump_commands(pilot, "step-4-run")
            for _ in range(10):
                await pilot.pause()
            panel = pilot.app.query_one(CommandsPanel)
            text = panel.rendered_text()
            # AC4: capacity 3 → the two OLDEST (step-0, step-1) scrolled off.
            assert "step-0-run" not in text
            assert "step-1-run" not in text
            # The three newest remain, newest-on-top.
            assert "step-4-run" in text
            assert "step-3-run" in text
            assert "step-2-run" in text
            assert text.index("step-4-run") < text.index("step-3-run")
            assert text.index("step-3-run") < text.index("step-2-run")


class TestMruClick:
    """Clicking a row in the MRU panel pins that file in the Diff panel."""

    async def test_click_mru_row_pins_diff_panel(self, tmp_path: Path):
        root = tmp_path / "projects"
        session = root / "proj" / "click.jsonl"
        clock = _ManualClock()
        cfg = _fixture_config(root)
        app = VisualizerApp(cfg, now=clock)
        async with app.run_test(size=(120, 40)) as pilot:
            # Append a real Edit event so the MRU list and diff queue populate.
            _append_line(
                session,
                _write_tool_line(
                    "Edit",
                    {
                        "file_path": "/repo/pinned_file.py",
                        "old_string": "a = 1",
                        "new_string": "a = 2",
                    },
                    session_id="clickSESS001",
                    cwd="/home/dev/clickproj",
                ),
            )
            # Wait for the file to appear in the MRU panel and diff panel.
            await _pump(pilot, "/repo/pinned_file.py")
            await _pump_diff(pilot, "pinned_file.py", clock=clock)

            # Simulate a mouse-down on the MRU panel at the first entry row
            # (y=2: title line at 0, blank line at 1, first entry at 2).
            # Uses mouse_down (not click) because under tmux the button-release
            # event is not forwarded so Click is never generated.
            mru_panel = pilot.app.query_one(MruFilesPanel)
            await pilot.mouse_down(mru_panel, offset=(10, 2))
            await pilot.pause()

            # The diff panel title must now show the pin indicator.
            diff_panel = pilot.app.query_one(DiffPanel)
            diff_text = diff_panel.rendered_text()
            assert "📌 pinned" in diff_text, (
                f"Expected '📌 pinned' in diff panel title after MRU click; "
                f"diff text was:\n{diff_text}"
            )

    async def test_click_wrapped_row_selects_correct_entry(self, tmp_path: Path):
        """Clicking below a wrapped entry correctly selects the next entry.

        Regression test for the naïve y-2 mapping that treated every physical
        terminal line as a separate MRU entry.  Long file paths wrap to multiple
        physical lines; without wrap-aware accounting, clicking on the first line
        of entry 1 would compute an out-of-range entry index (→ no pin at all).
        The wrap-aware scan accumulates each entry's physical line count and maps
        the click to the right entry.
        """
        root = tmp_path / "projects"
        session = root / "proj" / "wrap.jsonl"
        clock = _ManualClock()
        cfg = _fixture_config(root)
        app = VisualizerApp(cfg, now=clock)
        async with app.run_test(size=(120, 40)) as pilot:
            mru_panel = pilot.app.query_one(MruFilesPanel)
            diff_panel = pilot.app.query_one(DiffPanel)

            # file_a appended first → entry 1 (older) in the MRU list
            _append_line(
                session,
                _write_tool_line(
                    "Edit",
                    {
                        "file_path": "/repo/older.py",
                        "old_string": "a=1",
                        "new_string": "a=2",
                    },
                    session_id="wrapSESS1234",
                    cwd="/home/dev/wp",
                ),
            )
            # file_b appended second → entry 0 (newest); intentionally long so
            # its row wraps to ≥2 physical lines at content_width≈38
            file_b = "/repo/a_longer_path_for_wrap_testing.py"
            _append_line(
                session,
                _write_tool_line(
                    "Edit",
                    {"file_path": file_b, "old_string": "b=1", "new_string": "b=2"},
                    session_id="wrapSESS1234",
                    cwd="/home/dev/wp",
                ),
            )

            # Wait for both files to land in the MRU panel and diff queue.
            await _pump(pilot, "older.py")
            await _pump(pilot, "wrap_testing")
            await _pump_diff(pilot, "wrap_testing", clock=clock)

            rows = mru_panel._rows
            assert len(rows) >= 2, f"Expected ≥2 MRU rows, got {len(rows)}"
            content_width = mru_panel.content_size.width
            assert content_width > 0, "Panel not yet laid out — content_size.width=0"

            # Verify entry 0 (file_b, newest) actually wraps: its plain row must
            # need ≥2 physical lines at the measured content width.
            entry_0_plain = format_mru_row(rows[0])
            entry_0_lines = max(
                1, (len(entry_0_plain) + content_width - 1) // content_width
            )
            assert entry_0_lines >= 2, (
                f"Expected entry 0 to wrap ≥2 lines at content_width={content_width}; "
                f"got {entry_0_lines} (plain len={len(entry_0_plain)!r})"
            )

            # Click at the physical y-row where entry 1 (older.py) starts.
            # Old naïve code: entry_index = click_y - 2 = entry_0_lines
            #   → out of range (only 2 entries, indices 0 and 1) → no FileClicked
            # New wrap-aware scan: correctly maps to entry 1 → pins older.py
            click_y = 2 + entry_0_lines
            await pilot.mouse_down(mru_panel, offset=(10, click_y))
            await pilot.pause()

            diff_text = diff_panel.rendered_text()
            assert "📌 pinned" in diff_text, (
                f"Expected '📌 pinned' after clicking entry 1 at y={click_y} "
                f"(entry_0_lines={entry_0_lines}, content_width={content_width}); "
                f"diff text:\n{diff_text}"
            )
            assert (
                "older.py" in diff_text
            ), f"Expected 'older.py' (entry 1) to be pinned, but diff shows:\n{diff_text}"


class TestMruScroll:
    """Mouse-wheel scroll on the MRU panel adjusts the visible row window."""

    def _populated_panel(self, n: int = 6) -> tuple:
        """Return (panel, model) with ``n`` entries recorded."""
        model = MruModel(AppConfig(mru_max=50))
        for i in range(n):
            model.record(
                _file_event(
                    file_path=f"/repo/file_{i:02d}.py",
                    session_id=f"sess{i:04d}abcd",
                    project_tag="proj",
                )
            )
        panel = MruFilesPanel()
        panel.update_from_model(model)
        return panel, model

    def test_mru_scroll_down_moves_offset(self):
        panel, model = self._populated_panel(6)
        assert panel._scroll_offset == 0
        panel.on_mouse_scroll_down(None)
        assert panel._scroll_offset == 1

    def test_mru_scroll_up_clamps_at_zero(self):
        panel, model = self._populated_panel(6)
        assert panel._scroll_offset == 0
        panel.on_mouse_scroll_up(None)
        assert panel._scroll_offset == 0

    def test_mru_scroll_down_clamps_at_last_row(self):
        panel, model = self._populated_panel(4)
        rows = model.rows()
        # Scroll far more times than there are rows.
        for _ in range(20):
            panel.on_mouse_scroll_down(None)
        assert panel._scroll_offset <= len(rows) - 1

    def test_mru_scroll_hides_top_rows(self):
        panel, model = self._populated_panel(6)
        rows = model.rows()
        # rows() returns newest-first; rows[0] is the most-recently recorded.
        top_path = rows[0].file_path
        later_path = rows[2].file_path
        # Scroll down twice so the top entry is no longer in the visible slice.
        panel.on_mouse_scroll_down(None)
        panel.on_mouse_scroll_down(None)
        text = panel.rendered_text()
        assert top_path not in text
        assert later_path in text

    def test_mru_scroll_offset_resets_on_model_shrink(self):
        panel, model = self._populated_panel(10)
        # Scroll to offset 5.
        for _ in range(5):
            panel.on_mouse_scroll_down(None)
        assert panel._scroll_offset == 5
        # Replace with a 2-entry model.
        small_model = MruModel(AppConfig(mru_max=50))
        for i in range(2):
            small_model.record(
                _file_event(
                    file_path=f"/repo/small_{i}.py",
                    session_id=f"smsess{i:04d}",
                    project_tag="proj",
                )
            )
        panel.update_from_model(small_model)
        assert panel._scroll_offset <= 1


class TestPinCurrentAction:
    """Pressing `p` pins the file currently shown in the Diff panel."""

    async def test_p_key_pins_current_diff(self, tmp_path: Path):
        root = tmp_path / "projects"
        session = root / "proj" / "pin.jsonl"
        clock = _ManualClock()
        cfg = _fixture_config(root)
        app = VisualizerApp(cfg, now=clock)
        async with app.run_test(size=(120, 40)) as pilot:
            # Append a real Edit event so the diff queue has something to show.
            _append_line(
                session,
                _write_tool_line(
                    "Edit",
                    {
                        "file_path": "/repo/keyboard_pin.py",
                        "old_string": "x = 1",
                        "new_string": "x = 2",
                    },
                    session_id="pinKEYSESS01",
                    cwd="/home/dev/pinproj",
                ),
            )
            # Wait for the file to appear in the MRU panel and diff panel.
            await _pump(pilot, "/repo/keyboard_pin.py")
            await _pump_diff(pilot, "keyboard_pin.py", clock=clock)

            # Press `p` — the keyboard shortcut for pin_current.
            await pilot.press("p")
            await pilot.pause()

            # The diff panel title must now show the pin indicator.
            diff_panel = pilot.app.query_one(DiffPanel)
            diff_text = diff_panel.rendered_text()
            assert "📌 pinned" in diff_text, (
                f"Expected '📌 pinned' in diff panel title after pressing 'p'; "
                f"diff text was:\n{diff_text}"
            )


class TestDiffScroll:
    """Mouse-wheel scroll on the Diff panel advances the pinned diff's scroll offset."""

    async def test_mouse_wheel_scroll_changes_visible_segments(self, tmp_path: Path):
        from textual.events import MouseScrollDown

        root = tmp_path / "projects"
        session = root / "proj" / "scroll.jsonl"
        clock = _ManualClock()
        cfg = _fixture_config(root)
        app = VisualizerApp(cfg, now=clock)
        async with app.run_test(size=(120, 40)) as pilot:
            # Append a tall diff so there is something to scroll.
            tall_content = "\n".join(f"line{i}" for i in range(80))
            _append_line(
                session,
                _write_tool_line(
                    "Write",
                    {"file_path": "/repo/scroll_target.py", "content": tall_content},
                    session_id="scrollSESS01",
                    cwd="/home/dev/scrollproj",
                ),
            )
            # Wait for file to appear in MRU and diff.
            await _pump(pilot, "/repo/scroll_target.py")
            await _pump_diff(pilot, "scroll_target.py", clock=clock)

            # Pin the current diff via keyboard shortcut.
            await pilot.press("p")
            await pilot.pause()

            diff_panel = pilot.app.query_one(DiffPanel)
            assert (
                "📌 pinned" in diff_panel.rendered_text()
            ), "File must be pinned before testing scroll"

            # Fire a MouseScrollDown event through the Textual pilot mechanism.
            await pilot._post_mouse_events([MouseScrollDown], "#top-right")
            await pilot.pause()

            # The diff queue's _pin_scroll must be > 0 after a scroll-down.
            assert app._diff_queue._pin_scroll > 0, (
                f"Expected _pin_scroll > 0 after MouseScrollDown, "
                f"got {app._diff_queue._pin_scroll}"
            )


class TestVisualizerAppTeardown:
    """Regression: clean teardown — no deferred tick after panels are gone.

    Story #3 manual testing surfaced a ``NoMatches: No nodes match '#top-right'``
    when a ``set_interval`` tick fired AFTER the app unmounted (panels removed).
    These tests pin that the refresh timer is stopped on unmount AND that a stray
    tick is a defensive no-op, so the exception cannot recur — finalizing clean
    teardown for the whole 3-panel app.
    """

    async def test_two_app_runs_in_one_process_teardown_cleanly(self, tmp_path: Path):
        # Two independent run_test() lifecycles back-to-back in ONE process is
        # the original repro: the first app's deferred tick must not fire into
        # the (now torn-down) tree while the second app spins up.
        for i in range(2):
            root = tmp_path / f"projects-{i}"
            session = root / "proj" / "s.jsonl"
            _append_line(
                session,
                _write_tool_line(
                    "Bash", {"command": f"echo run-{i}"}, session_id=f"runSESS{i}"
                ),
            )
            cfg = _fixture_config(root)
            app = VisualizerApp(cfg)
            async with app.run_test(size=(100, 30)) as pilot:
                await _pump_commands(pilot, f"echo run-{i}")
            # Context exited → app unmounted.  Its refresh timer must be stopped.
            assert app._refresh_timer is not None
            assert app._refresh_timer._task is None

    async def test_refresh_timer_stopped_on_unmount(self, tmp_path: Path):
        cfg = _fixture_config(tmp_path / "projects")
        app = VisualizerApp(cfg)
        async with app.run_test() as pilot:
            await pilot.pause()
            # While running, the interval timer is live (has an active task).
            assert app._refresh_timer is not None
            assert app._refresh_timer._task is not None
        # After unmount the timer is stopped (task cleared) so no tick can fire.
        assert app._refresh_timer._task is None

    async def test_refresh_tick_after_teardown_is_noop(self, tmp_path: Path):
        cfg = _fixture_config(tmp_path / "projects")
        app = VisualizerApp(cfg)
        async with app.run_test() as pilot:
            await pilot.pause()
        # Panels are gone now.  A stray deferred tick must be a defensive no-op
        # (panel query misses → return early) rather than raising NoMatches.
        app._refresh_panels()  # must NOT raise


# ---------------------------------------------------------------------------
# Status bar pure renderer (render_status_bar + _fmt_rate)
# ---------------------------------------------------------------------------


def _snapshot(
    cpu_pct: float = 42.0,
    ram_pct: float = 61.0,
    ram_free_bytes: int = 4_000_000_000,
    disk_read_bps: float = 1_200_000.0,
    disk_write_bps: float = 340_000.0,
    net_down_bps: float = 4_200_000.0,
    net_up_bps: float = 128_000.0,
):
    from claude_visualizer.models.system_stats import SystemStatsSnapshot

    return SystemStatsSnapshot(
        cpu_pct=cpu_pct,
        ram_pct=ram_pct,
        ram_free_bytes=ram_free_bytes,
        disk_read_bps=disk_read_bps,
        disk_write_bps=disk_write_bps,
        net_down_bps=net_down_bps,
        net_up_bps=net_up_bps,
    )


class TestRenderStatusBar:
    """Tests 7-14: render_status_bar pure formatter — content and colour spans.

    RELOCATED from panels.py to monitors/zzz_machine_stats.py (AC4).
    Assertions are byte-identical to the pre-refactor tests; only the import
    source changes to prove the bundled monitor is functionally equivalent.
    """

    def test_render_status_bar_contains_cpu_pct(self):
        from claude_visualizer.monitors.zzz_machine_stats import render_status_bar

        text = render_status_bar(_snapshot(cpu_pct=42.0))
        assert "42%" in text.plain

    def test_render_status_bar_bar_length(self):
        from claude_visualizer.monitors.zzz_machine_stats import (
            _STATS_BAR_EMPTY,
            _STATS_BAR_FILL,
            _STATS_BAR_WIDTH,
            render_status_bar,
        )

        text = render_status_bar(_snapshot(cpu_pct=50.0))
        plain = text.plain
        bar_chars = set(_STATS_BAR_FILL + _STATS_BAR_EMPTY)
        runs = []
        run_len = 0
        for ch in plain:
            if ch in bar_chars:
                run_len += 1
            else:
                if run_len > 0:
                    runs.append(run_len)
                run_len = 0
        if run_len > 0:
            runs.append(run_len)
        assert len(runs) >= 2, "Expected at least 2 bar runs (CPU and RAM)"
        for r in runs:
            assert r == _STATS_BAR_WIDTH, f"bar run length {r} != {_STATS_BAR_WIDTH}"

    def test_render_status_bar_green_below_60(self):
        from claude_visualizer.monitors.zzz_machine_stats import (
            _STATS_GREEN,
            render_status_bar,
        )

        text = render_status_bar(_snapshot(cpu_pct=42.0))
        green_spans = [s for s in text.spans if _STATS_GREEN in str(s.style)]
        assert green_spans, "Expected green style for cpu_pct=42 (< 60%)"

    def test_render_status_bar_yellow_60_to_80(self):
        from claude_visualizer.monitors.zzz_machine_stats import (
            _STATS_YELLOW,
            render_status_bar,
        )

        text = render_status_bar(_snapshot(cpu_pct=70.0))
        yellow_spans = [s for s in text.spans if _STATS_YELLOW in str(s.style)]
        assert yellow_spans, "Expected yellow style for cpu_pct=70 (60–80%)"

    def test_render_status_bar_red_above_80(self):
        from claude_visualizer.monitors.zzz_machine_stats import (
            _STATS_RED,
            render_status_bar,
        )

        text = render_status_bar(_snapshot(cpu_pct=85.0))
        red_spans = [s for s in text.spans if _STATS_RED in str(s.style)]
        assert red_spans, "Expected red style for cpu_pct=85 (>= 80%)"

    def test_render_status_bar_contains_ram_free_label(self):
        from claude_visualizer.monitors.zzz_machine_stats import render_status_bar

        text = render_status_bar(_snapshot(ram_free_bytes=9_877_947_392))
        assert "free" in text.plain

    def test_render_status_bar_contains_disk_label(self):
        from claude_visualizer.monitors.zzz_machine_stats import render_status_bar

        text = render_status_bar(_snapshot())
        assert "Disk" in text.plain

    def test_render_status_bar_contains_net_label(self):
        from claude_visualizer.monitors.zzz_machine_stats import render_status_bar

        text = render_status_bar(_snapshot())
        assert "Net" in text.plain

    def test_render_status_bar_column_stability(self):
        """Anti-jitter: '│ Disk' and 'free' offsets are identical for all digit-count combos.

        Snapshots span the full range of variable digit-counts:
          cpu_pct  ∈ {5, 50, 100}
          ram_pct  ∈ {5, 50, 100}
          ram_free ∈ {512 MB (sub-G), 37.5 GB (large-G)}

        After the fixed-width fix every combination must render ' │ Disk'
        starting at the same byte offset and 'free' at the same offset,
        proving the columns never jitter horizontally.
        """
        from claude_visualizer.monitors.zzz_machine_stats import render_status_bar

        _512_MB = 512 * 1024 * 1024
        _37_5_GB = int(37.5 * 1024**3)

        snapshots = [
            _snapshot(cpu_pct=cpu, ram_pct=ram, ram_free_bytes=free_bytes)
            for cpu in (5.0, 50.0, 100.0)
            for ram in (5.0, 50.0, 100.0)
            for free_bytes in (_512_MB, _37_5_GB)
        ]

        plains = [render_status_bar(s).plain for s in snapshots]

        disk_offsets = [p.index(" │ Disk") for p in plains]
        free_offsets = [p.index(" free") for p in plains]

        assert (
            len(set(disk_offsets)) == 1
        ), f"' │ Disk' offset varies across snapshots: {disk_offsets}\n" + "\n".join(
            plains
        )
        assert (
            len(set(free_offsets)) == 1
        ), f"'free' offset varies across snapshots: {free_offsets}\n" + "\n".join(
            plains
        )


class TestFmtRate:
    """Tests 15-19: _fmt_rate auto-scaling and fixed-width padding.

    RELOCATED from panels.py to monitors/zzz_machine_stats.py (AC4).
    """

    def test_fmt_rate_bytes(self):
        from claude_visualizer.monitors.zzz_machine_stats import _fmt_rate

        result = _fmt_rate(512.0)
        assert "512" in result
        assert "B/s" in result

    def test_fmt_rate_kilobytes(self):
        from claude_visualizer.monitors.zzz_machine_stats import _fmt_rate

        result = _fmt_rate(1536.0)
        assert "1.5" in result
        assert "K/s" in result

    def test_fmt_rate_megabytes(self):
        from claude_visualizer.monitors.zzz_machine_stats import _fmt_rate

        result = _fmt_rate(1_572_864.0)
        assert "1.5" in result
        assert "M/s" in result

    def test_fmt_rate_gigabytes(self):
        from claude_visualizer.monitors.zzz_machine_stats import _fmt_rate

        result = _fmt_rate(1_610_612_736.0)
        assert "1.5" in result
        assert "G/s" in result

    def test_fmt_rate_padded_to_7_chars(self):
        from claude_visualizer.monitors.zzz_machine_stats import _fmt_rate

        for bps in [0.0, 512.0, 1536.0, 1_048_576.0, 1_073_741_824.0]:
            result = _fmt_rate(bps)
            assert (
                len(result) == 7
            ), f"_fmt_rate({bps!r}) = {result!r} len={len(result)}"


# ---------------------------------------------------------------------------
# MonitorBar pure renderer — render_monitor_bar (story #6, AC2)
# ---------------------------------------------------------------------------


class TestMonitorBarPure:
    """Pure render_monitor_bar formatter — suppression and ordering (AC2)."""

    def test_non_empty_lines_rendered_in_order(self):
        """N non-empty lines → Text whose .plain has exactly N lines in order."""
        result = render_monitor_bar(["line-A", "line-B", "line-C"])
        assert result.plain.splitlines() == ["line-A", "line-B", "line-C"]

    def test_empty_str_skipped(self):
        """Empty string entries are excluded from the rendered output (AC2)."""
        result = render_monitor_bar(["alpha", "", "beta"])
        assert result.plain.splitlines() == ["alpha", "beta"]

    def test_empty_rich_text_skipped(self):
        """Empty Text('') entries are excluded from the rendered output (AC2)."""
        from rich.text import Text as RichText

        result = render_monitor_bar(["alpha", RichText(""), "beta"])
        assert result.plain.splitlines() == ["alpha", "beta"]

    def test_all_empty_yields_empty_text(self):
        """All-suppressed list → empty Text (no rows in output)."""
        from rich.text import Text as RichText

        result = render_monitor_bar(["", RichText("")])
        assert result.plain == ""


# ---------------------------------------------------------------------------
# MonitorBar widget — display/collapse behavior (story #6, AC3)
# ---------------------------------------------------------------------------


class TestMonitorBarWidget:
    """MonitorBar widget display=True/False gating and row stacking (AC3)."""

    def test_n_active_display_true_correct_rows(self):
        """N non-empty lines → display=True, rendered_text has N rows (AC3)."""
        bar = MonitorBar()
        bar.update_from_lines(["row-1", "row-2", "row-3"])
        assert bar.display is True
        assert bar.rendered_text().splitlines() == ["row-1", "row-2", "row-3"]

    def test_all_suppressed_display_false(self):
        """All-empty input → display=False (bar collapses to 0 rows, AC3)."""
        from rich.text import Text as RichText

        bar = MonitorBar()
        bar.update_from_lines(["", RichText("")])
        assert bar.display is False

    def test_empty_list_display_false(self):
        """Empty monitor list (no monitors loaded) → display=False."""
        bar = MonitorBar()
        bar.update_from_lines([])
        assert bar.display is False


# ---------------------------------------------------------------------------
# MonitorBar no-wrap regression — long lines must NOT wrap to extra rows
# ---------------------------------------------------------------------------


class TestMonitorBarNoWrap:
    """Regression guard: render_monitor_bar must never wrap long lines.

    A long monitor line (> terminal width) must be clipped to the terminal
    width with an ellipsis, NOT wrapped to an extra row.  Before the fix
    (``out = Text()``), render_lines would produce 3 visual rows for a 2-monitor
    feed when one line exceeded 40 chars — the wrapped row pushed the second
    monitor off-screen.  After the fix (``out = Text(no_wrap=True,
    overflow="ellipsis")``), render_lines produces exactly 2 rows, the first
    cropped to ``≤ width`` characters ending in ``…``.
    """

    def test_nowrap_long_line_clips_to_width(self) -> None:
        """render_monitor_bar at a narrow Console width → 2 rows, long one cropped.

        This is the deterministic regression guard.  Against the old
        ``out = Text()`` it would produce 3 rows; with the fix it produces 2.
        """
        from rich.console import Console

        long_line = "A" * 60  # 60 chars, well past width=40
        result = render_monitor_bar([long_line, "SECOND-MONITOR"])

        console = Console(width=40, highlight=False, no_color=True)
        rows = console.render_lines(result, console.options.update(width=40), pad=False)

        # Must be exactly 2 visual rows — one per monitor, no wrap-overflow.
        assert len(rows) == 2, (
            f"Expected 2 visual rows (one per monitor) but got {len(rows)}. "
            f"A plain Text() would wrap the long line to an extra row. "
            f"Rows: {[repr(''.join(s.text for s in r)) for r in rows]}"
        )

        # First row must be cropped to ≤ 40 cells (Rich's ellipsis clips at width).
        first_row_text = "".join(s.text for s in rows[0])
        assert (
            len(first_row_text) <= 40
        ), f"Long row must be ≤ 40 chars; got {len(first_row_text)}: {first_row_text!r}"
        # The crop indicator is the Rich ellipsis character.
        assert first_row_text.endswith(
            "…"
        ), f"Cropped row must end with '…'; got: {first_row_text!r}"

        # Second row must contain the short monitor marker.
        second_row_text = "".join(s.text for s in rows[1])
        assert (
            "SECOND-MONITOR" in second_row_text
        ), f"Second row must contain 'SECOND-MONITOR'; got: {second_row_text!r}"


# ---------------------------------------------------------------------------
# MonitorBar UI — both monitors survive a narrow terminal (run_test harness)
# ---------------------------------------------------------------------------


class TestMonitorBarNarrowTerminal:
    """Real app harness: both monitors visible after layout at narrow width.

    Seeds a temp monitors_dir with two fixture monitors — one that returns
    a very long string, one that returns a short marker — boots the real
    VisualizerApp at ``size=(40, 12)`` and asserts:
    - Both monitors' content is present in MonitorBar.rendered_text() (content check).
    - The MonitorBar rendered text has exactly 2 lines (no wrap-overflow row).

    No network calls: fixture monitors do pure string arithmetic only.
    """

    async def test_both_monitors_survive_narrow_terminal(self, tmp_path: Path) -> None:
        """MonitorBar shows both monitors without wrap when one line is long."""
        # Create two fixture monitor .py files in a dedicated monitors_dir.
        monitors_dir = tmp_path / "monitors"
        monitors_dir.mkdir(parents=True, exist_ok=True)

        # Monitor A: returns a long line (70 chars), exceeds width=40.
        long_content = "LONG-MON:" + "X" * 61  # 70 chars total
        (monitors_dir / "aaa_long_monitor.py").write_text(
            f"class Monitor:\n"
            f"    def tick(self, now):\n"
            f"        return {long_content!r}\n"
        )

        # Monitor B: returns a short marker, should always be visible.
        (monitors_dir / "bbb_short_monitor.py").write_text(
            "class Monitor:\n"
            "    def tick(self, now):\n"
            "        return 'SHORT-MON-MARKER'\n"
        )

        root = tmp_path / "projects"
        cfg = _fixture_config(root, monitors_dir=monitors_dir)
        app = VisualizerApp(cfg)

        async with app.run_test(size=(40, 12)) as pilot:
            # Pump until MonitorBar is visible with both monitors' data.
            bar = pilot.app.query_one(MonitorBar)
            for _ in range(40):
                await pilot.pause()
                text = bar.rendered_text()
                if "LONG-MON" in text and "SHORT-MON-MARKER" in text:
                    break

            bar_text = bar.rendered_text()

            # Both monitors must be present.
            assert (
                "LONG-MON" in bar_text
            ), f"Long monitor must appear in MonitorBar; got: {bar_text!r}"
            assert (
                "SHORT-MON-MARKER" in bar_text
            ), f"Short monitor must appear in MonitorBar; got: {bar_text!r}"

            # Exactly 2 lines — the long line must NOT have wrapped to an extra row.
            lines = bar_text.splitlines()
            assert len(lines) == 2, (
                f"MonitorBar must show exactly 2 lines (one per monitor, no wrap); "
                f"got {len(lines)}: {lines!r}"
            )


# ---------------------------------------------------------------------------
# Regression: _fixture_config must not point at the real monitors directory
# ---------------------------------------------------------------------------


class TestFixtureConfigMonitorsDir:
    """Prove _fixture_config isolates monitor loading from the real user dir.

    The offender: _fixture_config used to omit monitors_dir, defaulting to
    ~/.claude-visualizer/monitors/.  Any VisualizerApp booted from such a
    config called MonitorRegistry.load() on the REAL seeded monitors (including
    proxmox_cluster.py), which then polled the live Proxmox cluster.

    Invariants asserted here:
    - _fixture_config().monitors_dir is NOT the real ~/.claude-visualizer/monitors/
    - A MonitorRegistry built from _fixture_config() loads ZERO monitors.
    """

    def test_fixture_config_monitors_dir_is_not_real_user_dir(
        self, tmp_path: Path
    ) -> None:
        """_fixture_config must point monitors_dir at a controlled dir, not the real one."""
        from claude_visualizer.models.monitor_registry import (
            MonitorRegistry,  # noqa: F401
        )

        real_user_monitors = Path.home() / ".claude-visualizer" / "monitors"
        cfg = _fixture_config(tmp_path / "projects")
        assert cfg.monitors_dir != real_user_monitors, (
            f"_fixture_config().monitors_dir must NOT be the real user monitors dir "
            f"{real_user_monitors!r}; got {cfg.monitors_dir!r}"
        )

    def test_fixture_config_loads_zero_monitors(self, tmp_path: Path) -> None:
        """MonitorRegistry built from _fixture_config must load ZERO monitors."""
        from claude_visualizer.models.monitor_registry import MonitorRegistry

        cfg = _fixture_config(tmp_path / "projects")
        registry = MonitorRegistry(cfg)
        registry.load()
        lines = registry.tick(0.0)
        assert lines == [], (
            f"Expected MonitorRegistry from _fixture_config to load 0 monitors; "
            f"got {len(lines)} line(s): {lines!r}. "
            f"monitors_dir was: {cfg.monitors_dir!r}"
        )


# ---------------------------------------------------------------------------
# Regression: network guard raises on non-loopback, allows loopback
# ---------------------------------------------------------------------------


class TestNetworkGuard:
    """Prove the conftest network guard works correctly.

    The autouse ``_block_live_network`` fixture patches socket.connect so any
    non-loopback AF_INET/AF_INET6 connect raises RuntimeError.  These tests
    verify both sides of that guard: blocked (non-loopback) and allowed
    (loopback classification logic).
    """

    def test_guard_raises_on_non_loopback_connect(self) -> None:
        """A non-loopback AF_INET connect must raise RuntimeError from the guard."""
        import socket as _socket

        sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        try:
            with pytest.raises(RuntimeError, match="live network connection"):
                # 192.0.2.1 is TEST-NET-1 (RFC 5737) — documentation-only,
                # guaranteed unreachable, but the guard fires BEFORE the kernel
                # even tries to connect.
                sock.connect(("192.0.2.1", 80))
        finally:
            sock.close()

    def test_guard_is_loopback_helper_classifies_correctly(self) -> None:
        """_is_loopback correctly identifies loopback vs non-loopback addresses."""
        from tests.conftest import _is_loopback

        # Loopback — must return True.
        assert _is_loopback(("127.0.0.1", 8080)) is True
        assert _is_loopback(("127.0.0.2", 1234)) is True  # full 127.x.x.x range
        assert _is_loopback(("::1", 80)) is True
        assert _is_loopback(("localhost", 80)) is True

        # Non-loopback — must return False.
        assert _is_loopback(("192.168.68.15", 8006)) is False  # Proxmox cluster
        assert _is_loopback(("192.0.2.1", 80)) is False  # TEST-NET-1
        assert _is_loopback(("8.8.8.8", 53)) is False
        assert _is_loopback(("10.0.0.1", 22)) is False

        # Non-tuple inputs — must return False safely (no crash).
        assert _is_loopback("/tmp/sock") is False  # AF_UNIX path string
        assert _is_loopback(None) is False
