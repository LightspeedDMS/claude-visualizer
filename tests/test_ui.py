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
    render_diff,
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
    timestamp: str = "2024-01-15T10:00:00.000Z",
) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "timestamp": timestamp,
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
        # Explicit distinct timestamps so the timestamp sort is deterministic:
        # b.py (13:45:08) is newer than a.py (13:45:07) → b appears first.
        t_a = datetime(2024, 1, 2, 13, 45, 7, tzinfo=timezone.utc)
        t_b = datetime(2024, 1, 2, 13, 45, 8, tzinfo=timezone.utc)
        model.record(_file_event(file_path="/r/a.py", session_id="aaaa0000", ts=t_a))
        model.record(_file_event(file_path="/r/b.py", session_id="bbbb1111", ts=t_b))
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
        # Explicit timestamps so a.py (older) sorts to index 1 (odd) reliably.
        from claude_visualizer.ui.panels import MRU_HIGHLIGHT_STYLE

        t_old = datetime(2024, 1, 2, 13, 45, 7, tzinfo=timezone.utc)
        t_new = datetime(2024, 1, 2, 13, 45, 8, tzinfo=timezone.utc)
        model = MruModel(AppConfig(mru_max=10))
        model.record(_file_event(file_path="/r/a.py", ts=t_old))  # row 1 (odd)
        model.record(_file_event(file_path="/r/b.py", ts=t_new))  # row 0 (even, newest)
        model.highlighted_key = ("/r/a.py", t_old)
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
        # Explicit timestamps so the long-path entry (older) sorts to index 1
        # (odd, zebra) and /r/b.py (newer) sorts to index 0 (even) reliably.
        t_old = datetime(2024, 1, 2, 13, 45, 7, tzinfo=timezone.utc)
        t_new = datetime(2024, 1, 2, 13, 45, 8, tzinfo=timezone.utc)
        model = MruModel(AppConfig(mru_max=10))
        # Record two entries so the second (odd) row gets a zebra span — the
        # long path is older so it ends up at index 1 (odd).
        model.record(
            _file_event(
                file_path="/very/long/path/that/exceeds/panel/width/filename.py",
                ts=t_old,
            )
        )
        model.record(
            _file_event(file_path="/r/b.py", ts=t_new)
        )  # newest → index 0 (even)
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
        text = render_commands(model, 0, width=80).plain
        assert COMMANDS_EMPTY_TEXT in text

    def test_commands_rendered_newest_on_top(self):
        model = CommandFeedModel(AppConfig(command_feed_max=10))
        model.record(_cmd_event(command="older-cmd"))
        model.record(_cmd_event(command="newer-cmd"))
        text = render_commands(model, 0, width=80).plain
        assert text.index("newer-cmd") < text.index("older-cmd")

    def test_no_dedup_both_identical_rows_rendered(self):
        model = CommandFeedModel(AppConfig(command_feed_max=10))
        model.record(_cmd_event(command="dup-cmd"))
        model.record(_cmd_event(command="dup-cmd"))
        text = render_commands(model, 0, width=80).plain
        assert text.count("dup-cmd") == 2

    def test_subagent_marker_present_for_subagent_row(self):
        model = CommandFeedModel(AppConfig(command_feed_max=10))
        model.record(_cmd_event(command="sub-cmd", is_subagent=True))
        assert SUBAGENT_MARKER in render_commands(model, 0, width=80).plain


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
            # AC2: all-green additions for a Write, no fabricated DEL lines.
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

            # file_a appended first → entry 1 (older) in the MRU list.
            # Explicit earlier timestamp so it sorts to index 1 deterministically.
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
                    timestamp="2024-01-15T10:00:00.000Z",
                ),
            )
            # file_b appended second → entry 0 (newest); intentionally long so
            # its row wraps to ≥2 physical lines at content_width≈38.
            # Explicit later timestamp so it sorts to index 0 deterministically.
            file_b = "/repo/a_longer_path_for_wrap_testing.py"
            _append_line(
                session,
                _write_tool_line(
                    "Edit",
                    {"file_path": file_b, "old_string": "b=1", "new_string": "b=2"},
                    session_id="wrapSESS1234",
                    cwd="/home/dev/wp",
                    timestamp="2024-01-15T10:00:01.000Z",
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

    def test_render_monitor_bar_width_truncates_long_line(self):
        """render_monitor_bar(width=40) truncates a 200-char line to ≤40 cells ending with …"""
        long_line = "M" * 200
        result = render_monitor_bar([long_line, "MON2"], width=40)
        lines = result.plain.splitlines()
        first = lines[0]
        # Truncated to at most 40 cells and ends with the ellipsis.
        assert len(first) <= 40, f"Expected ≤40 chars, got {len(first)}: {first!r}"
        assert first.endswith(
            TRUNCATION_ELLIPSIS
        ), f"Truncated line must end with '…'; got: {first!r}"

    def test_render_monitor_bar_width_preserves_two_logical_lines(self):
        """render_monitor_bar(width=40) with 2 monitors → exactly 2 logical lines."""
        long_line = "N" * 200
        result = render_monitor_bar([long_line, "MON2"], width=40)
        lines = result.plain.splitlines()
        assert (
            len(lines) == 2
        ), f"Expected exactly 2 logical lines; got {len(lines)}: {lines!r}"
        # Second line must contain the short monitor text.
        assert "MON2" in lines[1], f"Second line must be 'MON2'; got: {lines[1]!r}"

    def test_render_monitor_bar_no_width_arg_untruncated(self):
        """render_monitor_bar with no width arg preserves full untruncated content (backward compat)."""
        long_line = "P" * 200
        result = render_monitor_bar([long_line, "MON2"])
        lines = result.plain.splitlines()
        # Without width the content is untruncated (backward compatibility).
        assert (
            lines[0] == long_line
        ), f"Without width the long line must be untruncated; got: {lines[0]!r}"
        assert len(lines) == 2

    def test_render_monitor_bar_styled_text_truncation_preserves_spans_and_does_not_mutate(
        self,
    ):
        """Styled Text line: truncation keeps bold-red leading span; original not mutated.

        The proxmox monitor returns a rich.text.Text whose leading badge
        ``⚠ PVE UNREACHABLE`` is styled ``bold red``.  render_monitor_bar must:

        1. Produce exactly 2 logical lines (one per input item).
        2. Truncate the first line to ≤ 40 cells, ending with ``…``.
        3. Keep the ``bold red`` span covering the leading badge region.
        4. NOT mutate the original Text (no cross-tick corruption).
        """
        from rich.console import Console
        from rich.text import Text as RichText

        # Build the styled input — a leading bold-red badge + 200 filler chars.
        styled_line = RichText()
        styled_line.append("⚠ PVE UNREACHABLE", style="bold red")
        styled_line.append(" │ " + "x" * 200)
        original_plain_len = len(styled_line.plain)
        original_spans = list(styled_line.spans)  # snapshot for mutation check

        result = render_monitor_bar([styled_line, "MON2"], width=40)

        # -- Assertion 1: exactly 2 logical lines ----------------------------
        logical_lines = result.plain.splitlines()
        assert len(logical_lines) == 2, (
            f"Expected exactly 2 logical lines; got {len(logical_lines)}: "
            f"{logical_lines!r}"
        )

        # -- Assertion 2: first line ≤ 40 cells and ends with … --------------
        first_plain = logical_lines[0]
        assert (
            len(first_plain) <= 40
        ), f"First line must be ≤ 40 cells; got {len(first_plain)}: {first_plain!r}"
        assert first_plain.endswith(
            TRUNCATION_ELLIPSIS
        ), f"First line must end with '…'; got: {first_plain!r}"

        # -- Assertion 3: leading bold-red span survives in the output --------
        console = Console()
        style_at_0 = result.get_style_at_offset(console, 0)
        assert style_at_0.bold, (
            "Leading span must still be bold after truncation; "
            f"style at offset 0: {style_at_0!r}"
        )
        assert (
            style_at_0.color is not None and "red" in str(style_at_0.color).lower()
        ), (
            "Leading span must still be red after truncation; "
            f"color at offset 0: {style_at_0.color!r}"
        )

        # -- Assertion 4: original Text NOT mutated ---------------------------
        assert len(styled_line.plain) == original_plain_len, (
            "render_monitor_bar must not mutate the input Text's length; "
            f"original was {original_plain_len}, now {len(styled_line.plain)}"
        )
        assert list(styled_line.spans) == original_spans, (
            "render_monitor_bar must not mutate the input Text's spans; "
            f"original: {original_spans!r}, now: {list(styled_line.spans)!r}"
        )


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

    async def test_monitor_bar_height_equals_active_monitor_count(
        self, tmp_path: Path
    ) -> None:
        """MonitorBar widget size.height must equal the number of active monitors.

        This is the REAL regression guard: it asserts the ACTUAL Textual widget
        height, not just logical newlines in rendered_text().  Before the
        truncation fix (``render_monitor_bar(width=...)``), a 200-char monitor
        line at terminal width=40 causes Textual to wrap it to ~6 extra rows,
        making ``MonitorBar.size.height ≈ 7`` instead of 2.  After the fix
        (each line truncated to content_size.width before joining), the widget
        height equals exactly the number of active monitors (2).

        No network calls: fixture monitors return pure strings.
        """
        monitors_dir = tmp_path / "monitors_height"
        monitors_dir.mkdir(parents=True, exist_ok=True)

        # Monitor A: returns a 200-char line — far wider than terminal width=40.
        # Before the fix this single logical line wraps to ~6 visual rows in
        # Textual's compositor, inflating MonitorBar.size.height to ~7.
        long_line = "HLONG:" + "Y" * 200
        (monitors_dir / "aaa_long.py").write_text(
            "class Monitor:\n"
            "    def tick(self, now):\n"
            f"        return {long_line!r}\n"
        )

        # Monitor B: short marker — proves the second monitor is not pushed off.
        (monitors_dir / "zzz_short.py").write_text(
            "class Monitor:\n"
            "    def tick(self, now):\n"
            "        return 'HSHORT-MARKER'\n"
        )

        root = tmp_path / "projects"
        cfg = _fixture_config(root, monitors_dir=monitors_dir)
        app = VisualizerApp(cfg)

        async with app.run_test(size=(40, 12)) as pilot:
            bar = pilot.app.query_one(MonitorBar)
            # Pump until BOTH monitors are populated AND the widget height converges
            # to 2.  Two phases are needed:
            #
            # Phase 1: content appears (monitors tick and update_from_lines fires).
            #          At this point content_size.width may still be measured at the
            #          pre-layout size, so the truncation uses _MONITOR_BAR_DEFAULT_WIDTH
            #          and height may still be > 2.
            #
            # Phase 2: the layout=True refresh triggers Textual to re-measure with the
            #          real content_size.width (38 at size=(40,12)), and height settles
            #          to 2.  We keep pumping until this converges (bounded loop).
            for _ in range(80):
                await pilot.pause()
                t = bar.rendered_text()
                if "HLONG" in t and "HSHORT" in t and bar.size.height == 2:
                    break

            # The ACTUAL widget height must equal the number of active monitors (2).
            # This fails on the pre-fix code where height ≈ 7 due to line-wrapping.
            assert bar.size.height == 2, (
                f"MonitorBar.size.height must equal the active monitor count (2), "
                f"not {bar.size.height}. A height > 2 means long lines are wrapping "
                f"inside the widget — each line must be truncated to content_size.width "
                f"before joining so Textual's compositor sees at most 1 row per monitor. "
                f"rendered_text: {bar.rendered_text()!r}"
            )

            # Short monitor marker must still be present (not pushed off by wrapping).
            assert "HSHORT-MARKER" in bar.rendered_text(), (
                f"Short monitor must be visible in MonitorBar.rendered_text(); "
                f"got: {bar.rendered_text()!r}"
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


class TestShiftArrowSplitters:
    """Shift+Arrow resizes splitters; plain arrows fall through (Feature 1)."""

    async def test_shift_right_grows_mru_width(self, tmp_path: Path):
        cfg = _fixture_config(tmp_path / "projects")
        app = VisualizerApp(cfg)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            before = app._mru_width
            await pilot.press("shift+right")
            await pilot.pause()
            assert (
                app._mru_width > before
            ), f"shift+right must grow _mru_width; before={before}, after={app._mru_width}"

    async def test_shift_left_shrinks_mru_width(self, tmp_path: Path):
        cfg = _fixture_config(tmp_path / "projects")
        app = VisualizerApp(cfg)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            before = app._mru_width
            await pilot.press("shift+left")
            await pilot.pause()
            assert (
                app._mru_width < before
            ), f"shift+left must shrink _mru_width; before={before}, after={app._mru_width}"

    async def test_shift_up_grows_bottom_height(self, tmp_path: Path):
        cfg = _fixture_config(tmp_path / "projects")
        app = VisualizerApp(cfg)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            before = app.query_one("#bottom").size.height
            await pilot.press("shift+up")
            await pilot.pause()
            after = app.query_one("#bottom").size.height
            assert (
                after > before
            ), f"shift+up must grow bottom height; before={before}, after={after}"

    async def test_shift_down_shrinks_bottom_height(self, tmp_path: Path):
        cfg = _fixture_config(tmp_path / "projects")
        app = VisualizerApp(cfg)
        async with app.run_test(size=(120, 40)) as pilot:
            # First grow so we can shrink
            await pilot.press("shift+up")
            await pilot.pause()
            before = app.query_one("#bottom").size.height
            await pilot.press("shift+down")
            await pilot.pause()
            after = app.query_one("#bottom").size.height
            assert (
                after < before
            ), f"shift+down must shrink bottom height; before={before}, after={after}"

    async def test_plain_right_does_not_resize_splitter(self, tmp_path: Path):
        cfg = _fixture_config(tmp_path / "projects")
        app = VisualizerApp(cfg)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            before = app._mru_width
            await pilot.press("right")
            await pilot.pause()
            assert (
                app._mru_width == before
            ), f"plain right must NOT resize splitter; before={before}, after={app._mru_width}"

    async def test_plain_up_does_not_resize_splitter(self, tmp_path: Path):
        cfg = _fixture_config(tmp_path / "projects")
        app = VisualizerApp(cfg)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            before = app.query_one("#bottom").size.height
            await pilot.press("up")
            await pilot.pause()
            after = app.query_one("#bottom").size.height
            assert (
                after == before
            ), f"plain up must NOT resize splitter; before={before}, after={after}"


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


# ---------------------------------------------------------------------------
# Feature 2 — Focusable panels (can_focus, Tab cycle, click-to-focus, ↑/↓)
# ---------------------------------------------------------------------------


class TestFocusablePanels:
    """Panels are focusable: Tab cycles them, click focuses, ↑/↓ scroll."""

    def test_mru_panel_can_focus(self):
        """MruFilesPanel must declare can_focus = True."""
        assert MruFilesPanel.can_focus is True

    def test_diff_panel_can_focus(self):
        """DiffPanel must declare can_focus = True."""
        assert DiffPanel.can_focus is True

    def test_commands_panel_can_focus(self):
        """CommandsPanel must declare can_focus = True."""
        assert CommandsPanel.can_focus is True

    async def test_tab_cycles_focus_mru_to_diff_to_commands(self, tmp_path: Path):
        """Tab moves focus MRU → Diff → Commands in order."""
        cfg = _fixture_config(tmp_path / "projects")
        app = VisualizerApp(cfg)
        async with app.run_test(size=(120, 40)) as pilot:
            # Focus the MRU panel first.
            mru = pilot.app.query_one(MruFilesPanel)
            mru.focus()
            await pilot.pause()
            assert (
                pilot.app.focused is mru
            ), "MRU must be focused after explicit focus()"

            # Tab → Diff panel.
            await pilot.press("tab")
            await pilot.pause()
            diff = pilot.app.query_one(DiffPanel)
            assert pilot.app.focused is diff, (
                f"After tab from MRU, expected DiffPanel focused; "
                f"got {type(pilot.app.focused).__name__}"
            )

            # Tab → Commands panel.
            await pilot.press("tab")
            await pilot.pause()
            commands = pilot.app.query_one(CommandsPanel)
            assert pilot.app.focused is commands, (
                f"After tab from Diff, expected CommandsPanel focused; "
                f"got {type(pilot.app.focused).__name__}"
            )

    async def test_shift_tab_reverses_focus_cycle(self, tmp_path: Path):
        """Shift+Tab reverses the Tab focus cycle."""
        cfg = _fixture_config(tmp_path / "projects")
        app = VisualizerApp(cfg)
        async with app.run_test(size=(120, 40)) as pilot:
            commands = pilot.app.query_one(CommandsPanel)
            commands.focus()
            await pilot.pause()
            assert pilot.app.focused is commands

            # Shift+Tab → Diff.
            await pilot.press("shift+tab")
            await pilot.pause()
            diff = pilot.app.query_one(DiffPanel)
            assert pilot.app.focused is diff, (
                f"After shift+tab from Commands, expected DiffPanel focused; "
                f"got {type(pilot.app.focused).__name__}"
            )

    async def test_mouse_down_focuses_diff_panel(self, tmp_path: Path):
        """Clicking the Diff panel focuses it."""
        cfg = _fixture_config(tmp_path / "projects")
        app = VisualizerApp(cfg)
        async with app.run_test(size=(120, 40)) as pilot:
            diff = pilot.app.query_one(DiffPanel)
            await pilot.mouse_down(diff, offset=(5, 2))
            await pilot.pause()
            assert pilot.app.focused is diff, (
                f"mouse_down on DiffPanel must focus it; "
                f"got {type(pilot.app.focused).__name__}"
            )

    async def test_mouse_down_focuses_commands_panel(self, tmp_path: Path):
        """Clicking the Commands panel focuses it."""
        cfg = _fixture_config(tmp_path / "projects")
        app = VisualizerApp(cfg)
        async with app.run_test(size=(120, 40)) as pilot:
            cmd_panel = pilot.app.query_one(CommandsPanel)
            await pilot.mouse_down(cmd_panel, offset=(5, 2))
            await pilot.pause()
            assert pilot.app.focused is cmd_panel, (
                f"mouse_down on CommandsPanel must focus it; "
                f"got {type(pilot.app.focused).__name__}"
            )

    async def test_focus_border_no_layout_shift(self, tmp_path: Path):
        """content_size must be identical focused vs unfocused (no layout shift)."""
        cfg = _fixture_config(tmp_path / "projects")
        app = VisualizerApp(cfg)
        async with app.run_test(size=(120, 40)) as pilot:
            mru = pilot.app.query_one(MruFilesPanel)
            diff = pilot.app.query_one(DiffPanel)
            # Measure content_size while MRU is NOT focused.
            diff.focus()
            await pilot.pause()
            await pilot.pause()
            unfocused_size = mru.content_size
            # Now focus MRU.
            mru.focus()
            await pilot.pause()
            await pilot.pause()
            focused_size = mru.content_size
            assert focused_size == unfocused_size, (
                f"content_size must not change on focus: "
                f"unfocused={unfocused_size}, focused={focused_size}"
            )

    async def test_mru_down_key_increases_scroll_offset(self, tmp_path: Path):
        """↓ on focused MRU panel increases _scroll_offset."""
        root = tmp_path / "projects"
        session = root / "proj" / "mru_scroll.jsonl"
        cfg = _fixture_config(root)
        app = VisualizerApp(cfg)
        async with app.run_test(size=(120, 40)) as pilot:
            # Seed several MRU entries so there is something to scroll.
            for i in range(8):
                _append_line(
                    session,
                    _write_tool_line(
                        "Write",
                        {"file_path": f"/repo/file_{i:02d}.py", "content": "x"},
                        session_id=f"mruSESS{i:04d}",
                        cwd="/home/dev/mruproj",
                    ),
                )
            await _pump(pilot, "/repo/file_07.py")

            mru = pilot.app.query_one(MruFilesPanel)
            mru.focus()
            await pilot.pause()
            before = mru._scroll_offset
            await pilot.press("down")
            await pilot.pause()
            assert mru._scroll_offset > before, (
                f"↓ on focused MRU must increase _scroll_offset; "
                f"before={before}, after={mru._scroll_offset}"
            )

    async def test_mru_up_key_decreases_scroll_offset_floor_zero(self, tmp_path: Path):
        """↑ on focused MRU panel decreases _scroll_offset, floored at 0."""
        root = tmp_path / "projects"
        session = root / "proj" / "mru_up.jsonl"
        cfg = _fixture_config(root)
        app = VisualizerApp(cfg)
        async with app.run_test(size=(120, 40)) as pilot:
            for i in range(6):
                _append_line(
                    session,
                    _write_tool_line(
                        "Write",
                        {"file_path": f"/repo/up_{i:02d}.py", "content": "x"},
                        session_id=f"upSESS{i:04d}",
                        cwd="/home/dev/uproj",
                    ),
                )
            await _pump(pilot, "/repo/up_05.py")

            mru = pilot.app.query_one(MruFilesPanel)
            mru.focus()
            # Scroll down first so there is room to scroll up.
            await pilot.press("down")
            await pilot.press("down")
            await pilot.pause()
            assert mru._scroll_offset >= 2

            # Now scroll up — offset should decrease.
            await pilot.press("up")
            await pilot.pause()
            assert mru._scroll_offset < 2, (
                f"↑ on focused MRU must decrease _scroll_offset; "
                f"after two down + one up, got {mru._scroll_offset}"
            )

            # At offset 0, ↑ does not go negative.
            mru._scroll_offset = 0
            await pilot.press("up")
            await pilot.pause()
            assert mru._scroll_offset == 0, "↑ at offset 0 must stay at 0"

    async def test_diff_down_key_only_scrolls_when_pinned(self, tmp_path: Path):
        """↓ on focused Diff panel does nothing when unpinned; scrolls when pinned."""
        root = tmp_path / "projects"
        session = root / "proj" / "diff_key.jsonl"
        clock = _ManualClock()
        cfg = _fixture_config(root)
        app = VisualizerApp(cfg, now=clock)
        async with app.run_test(size=(120, 40)) as pilot:
            tall_content = "\n".join(f"line{i}" for i in range(80))
            _append_line(
                session,
                _write_tool_line(
                    "Write",
                    {"file_path": "/repo/diff_key_target.py", "content": tall_content},
                    session_id="diffKeySESS01",
                    cwd="/home/dev/diffkeyproj",
                ),
            )
            await _pump(pilot, "/repo/diff_key_target.py")
            await _pump_diff(pilot, "diff_key_target.py", clock=clock)

            diff = pilot.app.query_one(DiffPanel)
            diff.focus()
            await pilot.pause()

            # Unpinned: ↓ should not change pin_scroll (scroll_pin_by is no-op
            # when unpinned).
            pin_scroll_before = app._diff_queue._pin_scroll
            await pilot.press("down")
            await pilot.pause()
            assert app._diff_queue._pin_scroll == pin_scroll_before, (
                f"↓ on UNPINNED diff must not change _pin_scroll; "
                f"before={pin_scroll_before}, after={app._diff_queue._pin_scroll}"
            )

            # Pin the diff and verify ↓ advances scroll.
            await pilot.press("p")
            await pilot.pause()
            assert "📌 pinned" in diff.rendered_text()

            pin_scroll_before = app._diff_queue._pin_scroll
            await pilot.press("down")
            await pilot.pause()
            assert app._diff_queue._pin_scroll > pin_scroll_before, (
                f"↓ on PINNED diff must advance _pin_scroll; "
                f"before={pin_scroll_before}, after={app._diff_queue._pin_scroll}"
            )

    async def test_diff_up_key_when_pinned_decreases_scroll(self, tmp_path: Path):
        """↑ on focused pinned Diff panel decreases _pin_scroll, floor 0."""
        root = tmp_path / "projects"
        session = root / "proj" / "diff_up.jsonl"
        clock = _ManualClock()
        cfg = _fixture_config(root)
        app = VisualizerApp(cfg, now=clock)
        async with app.run_test(size=(120, 40)) as pilot:
            tall_content = "\n".join(f"line{i}" for i in range(80))
            _append_line(
                session,
                _write_tool_line(
                    "Write",
                    {"file_path": "/repo/diff_up_target.py", "content": tall_content},
                    session_id="diffUpSESS01",
                    cwd="/home/dev/diffupproj",
                ),
            )
            await _pump(pilot, "/repo/diff_up_target.py")
            await _pump_diff(pilot, "diff_up_target.py", clock=clock)

            diff = pilot.app.query_one(DiffPanel)
            diff.focus()

            # Pin and scroll down first.
            await pilot.press("p")
            await pilot.pause()
            assert "📌 pinned" in diff.rendered_text()

            await pilot.press("down")
            await pilot.press("down")
            await pilot.pause()
            assert app._diff_queue._pin_scroll >= 2

            # ↑ on pinned diff decreases _pin_scroll.
            pin_scroll_before = app._diff_queue._pin_scroll
            await pilot.press("up")
            await pilot.pause()
            assert app._diff_queue._pin_scroll < pin_scroll_before, (
                f"↑ on PINNED diff must decrease _pin_scroll; "
                f"before={pin_scroll_before}, after={app._diff_queue._pin_scroll}"
            )


# ---------------------------------------------------------------------------
# Feature 3 — Commands panel scroll + follow + autoscroll-follow
# ---------------------------------------------------------------------------


class TestCommandsScroll:
    """Commands panel scroll, follow flag, wheel, and autoscroll-follow."""

    def _populate_commands(self, n: int, root: Path) -> Path:
        """Write n Bash JSONL lines to a temp session; return the session path."""
        session = root / "proj" / "cmd_scroll.jsonl"
        for i in range(n):
            _append_line(
                session,
                _write_tool_line(
                    "Bash",
                    {"command": f"cmd-scroll-{i:02d}"},
                    session_id=f"cmdSCROLL{i:04d}",
                    cwd="/home/dev/scrollcmd",
                ),
            )
        return session

    def test_render_commands_with_scroll_offset_zero_shows_newest(self):
        """render_commands(model, scroll_offset=0, width) shows newest at top."""
        model = CommandFeedModel(AppConfig(command_feed_max=10))
        model.record(_cmd_event(command="older-cmd"))
        model.record(_cmd_event(command="newer-cmd"))
        text = render_commands(model, 0, 80).plain
        assert text.index("newer-cmd") < text.index("older-cmd")

    def test_render_commands_with_scroll_offset_hides_newest(self):
        """render_commands(model, scroll_offset=1, width) skips the newest row."""
        model = CommandFeedModel(AppConfig(command_feed_max=10))
        model.record(_cmd_event(command="oldest-cmd"))
        model.record(_cmd_event(command="middle-cmd"))
        model.record(_cmd_event(command="newest-cmd"))
        # offset=1 → skip newest-cmd (rows()[0]) → start from middle-cmd.
        text = render_commands(model, 1, 80).plain
        assert "newest-cmd" not in text
        assert "middle-cmd" in text
        assert "oldest-cmd" in text

    def test_commands_panel_initial_scroll_offset_zero(self):
        """CommandsPanel starts with _scroll_offset=0 and _follow=True."""
        panel = CommandsPanel()
        assert panel._scroll_offset == 0
        assert panel._follow is True

    def test_commands_panel_follow_true_keeps_offset_zero_on_new_record(self):
        """When _follow=True, a new record via update_from_model keeps offset=0."""
        panel = CommandsPanel()
        model = CommandFeedModel(AppConfig(command_feed_max=10))
        for i in range(5):
            model.record(_cmd_event(command=f"cmd-{i}"))
        panel.update_from_model(model, 80)
        assert panel._scroll_offset == 0
        # Add one more and update — offset stays 0 (follow=True).
        model.record(_cmd_event(command="cmd-5"))
        panel.update_from_model(model, 80)
        assert (
            panel._scroll_offset == 0
        ), "_follow=True must keep offset at 0 on new record"

    def test_commands_panel_scroll_down_increases_offset_and_disables_follow(self):
        """_scroll_commands(+1) increases offset and sets _follow=False."""
        panel = CommandsPanel()
        model = CommandFeedModel(AppConfig(command_feed_max=10))
        for i in range(5):
            model.record(_cmd_event(command=f"cmd-{i}"))
        panel.update_from_model(model, 80)
        assert panel._scroll_offset == 0
        panel._scroll_commands(1)
        assert panel._scroll_offset == 1
        assert panel._follow is False

    def test_commands_panel_scroll_back_to_zero_reenables_follow(self):
        """Scrolling back to offset 0 re-enables _follow."""
        panel = CommandsPanel()
        model = CommandFeedModel(AppConfig(command_feed_max=10))
        for i in range(5):
            model.record(_cmd_event(command=f"cmd-{i}"))
        panel.update_from_model(model, 80)
        panel._scroll_commands(1)
        assert panel._follow is False
        panel._scroll_commands(-1)
        assert panel._scroll_offset == 0
        assert panel._follow is True

    def test_commands_panel_follow_false_keeps_offset_on_new_record(self):
        """When _follow=False, a new record does NOT reset offset to 0."""
        panel = CommandsPanel()
        model = CommandFeedModel(AppConfig(command_feed_max=10))
        for i in range(5):
            model.record(_cmd_event(command=f"cmd-{i}"))
        panel.update_from_model(model, 80)
        panel._scroll_commands(2)
        assert panel._scroll_offset == 2
        assert panel._follow is False
        # Add a new command and update — offset must stay at 2, not reset.
        model.record(_cmd_event(command="cmd-5"))
        panel.update_from_model(model, 80)
        assert (
            panel._scroll_offset == 2
        ), "_follow=False must preserve offset when a new command arrives"

    def test_commands_scroll_clamps_at_list_length_minus_one(self):
        """_scroll_commands clamps offset to len(rows)-1."""
        panel = CommandsPanel()
        model = CommandFeedModel(AppConfig(command_feed_max=10))
        for i in range(3):
            model.record(_cmd_event(command=f"cmd-{i}"))
        panel.update_from_model(model, 80)
        for _ in range(20):
            panel._scroll_commands(1)
        assert panel._scroll_offset <= 2  # max index = 3-1

    def test_commands_wheel_up_calls_scroll_minus_one(self):
        """on_mouse_scroll_up scrolls up (offset -1)."""
        panel = CommandsPanel()
        model = CommandFeedModel(AppConfig(command_feed_max=10))
        for i in range(5):
            model.record(_cmd_event(command=f"cmd-{i}"))
        panel.update_from_model(model, 80)
        panel._scroll_commands(3)  # start at offset 3
        panel.on_mouse_scroll_up(None)
        assert panel._scroll_offset == 2

    def test_commands_wheel_down_calls_scroll_plus_one(self):
        """on_mouse_scroll_down scrolls down (offset +1)."""
        panel = CommandsPanel()
        model = CommandFeedModel(AppConfig(command_feed_max=10))
        for i in range(5):
            model.record(_cmd_event(command=f"cmd-{i}"))
        panel.update_from_model(model, 80)
        panel.on_mouse_scroll_down(None)
        assert panel._scroll_offset == 1

    async def test_commands_down_key_increases_scroll_offset(self, tmp_path: Path):
        """↓ on focused Commands panel increases _scroll_offset."""
        root = tmp_path / "projects"
        self._populate_commands(8, root)
        cfg = _fixture_config(root)
        app = VisualizerApp(cfg)
        async with app.run_test(size=(120, 40)) as pilot:
            text = await _pump_commands(pilot, "cmd-scroll-07")
            assert "cmd-scroll-07" in text

            cmd_panel = pilot.app.query_one(CommandsPanel)
            cmd_panel.focus()
            await pilot.pause()
            before = cmd_panel._scroll_offset
            await pilot.press("down")
            await pilot.pause()
            assert cmd_panel._scroll_offset > before, (
                f"↓ on focused Commands must increase _scroll_offset; "
                f"before={before}, after={cmd_panel._scroll_offset}"
            )

    async def test_commands_up_key_decreases_scroll_offset(self, tmp_path: Path):
        """↑ on focused Commands panel decreases _scroll_offset, floor 0."""
        root = tmp_path / "projects"
        self._populate_commands(8, root)
        cfg = _fixture_config(root)
        app = VisualizerApp(cfg)
        async with app.run_test(size=(120, 40)) as pilot:
            text = await _pump_commands(pilot, "cmd-scroll-07")
            assert "cmd-scroll-07" in text

            cmd_panel = pilot.app.query_one(CommandsPanel)
            cmd_panel.focus()
            await pilot.pause()

            # Scroll down first so there is room to scroll up.
            await pilot.press("down")
            await pilot.press("down")
            await pilot.pause()
            assert cmd_panel._scroll_offset >= 2

            before = cmd_panel._scroll_offset
            await pilot.press("up")
            await pilot.pause()
            assert cmd_panel._scroll_offset < before, (
                f"↑ on focused Commands must decrease _scroll_offset; "
                f"before={before}, after={cmd_panel._scroll_offset}"
            )

            # At offset 0, ↑ must not go negative.
            cmd_panel._scroll_offset = 0
            cmd_panel._follow = True
            await pilot.press("up")
            await pilot.pause()
            assert cmd_panel._scroll_offset == 0, "↑ at offset 0 must stay at 0"

    async def test_commands_scroll_follow_new_command_does_not_reset_when_unfollowed(
        self, tmp_path: Path
    ):
        """When user has scrolled (follow=False), a new command preserves offset."""
        root = tmp_path / "projects"
        session = root / "proj" / "follow_test.jsonl"
        cfg = _fixture_config(root)
        app = VisualizerApp(cfg)
        async with app.run_test(size=(120, 40)) as pilot:
            for i in range(6):
                _append_line(
                    session,
                    _write_tool_line(
                        "Bash",
                        {"command": f"follow-cmd-{i:02d}"},
                        session_id=f"followSESS{i:04d}",
                        cwd="/home/dev/followproj",
                    ),
                )
            await _pump_commands(pilot, "follow-cmd-05")

            cmd_panel = pilot.app.query_one(CommandsPanel)
            cmd_panel.focus()
            await pilot.pause()
            # Scroll down — disables follow.
            await pilot.press("down")
            await pilot.pause()
            assert cmd_panel._follow is False
            scroll_pos = cmd_panel._scroll_offset
            assert scroll_pos > 0

            # Append a new command — follow=False means offset must NOT reset.
            _append_line(
                session,
                _write_tool_line(
                    "Bash",
                    {"command": "follow-cmd-NEW"},
                    session_id="followSESS9999",
                    cwd="/home/dev/followproj",
                ),
            )
            await _pump_commands(pilot, "follow-cmd-NEW")
            # Offset must not have been reset to 0.
            assert cmd_panel._scroll_offset == scroll_pos, (
                f"follow=False must preserve scroll pos when new cmd arrives; "
                f"expected {scroll_pos}, got {cmd_panel._scroll_offset}"
            )


# ---------------------------------------------------------------------------
# Feature 4 — Title-highlight on focus (bold reverse on the focused panel title)
# ---------------------------------------------------------------------------


def _has_reverse_on_title(text, title: str) -> bool:
    """Return True iff the Rich Text has a 'reverse' span that covers the title."""
    pos = text.plain.find(title)
    if pos < 0:
        return False
    return any(
        "reverse" in str(s.style) and s.start <= pos and s.end >= pos + len(title)
        for s in text.spans
    )


class TestTitleHighlightPure:
    """Pure renderer tests: focused=True/False controls 'reverse' on panel titles."""

    @pytest.mark.parametrize("focused,expect_reverse", [(True, True), (False, False)])
    def test_render_mru_title_style(self, focused, expect_reverse):
        """render_mru focused=True→reverse on title; False→no reverse."""
        from claude_visualizer.ui.panels import MRU_TITLE

        model = MruModel(AppConfig(mru_max=10))
        text = render_mru(model, 0, 0, focused=focused)
        result = _has_reverse_on_title(text, MRU_TITLE)
        assert result == expect_reverse, (
            f"render_mru(focused={focused}) reverse={result}, expected {expect_reverse}; "
            f"spans: {text.spans}"
        )

    def test_render_mru_default_no_reverse(self):
        """render_mru() without focused kwarg defaults to no reverse on title."""
        from claude_visualizer.ui.panels import MRU_TITLE

        text = render_mru(MruModel(AppConfig(mru_max=10)))
        assert not _has_reverse_on_title(text, MRU_TITLE)

    @pytest.mark.parametrize("focused,expect_reverse", [(True, True), (False, False)])
    def test_render_diff_title_style(self, focused, expect_reverse):
        """render_diff focused=True→reverse on title; False→no reverse."""
        from claude_visualizer.ui.panels import DIFF_TITLE

        text = render_diff(None, focused=focused)
        result = _has_reverse_on_title(text, DIFF_TITLE)
        assert result == expect_reverse, (
            f"render_diff(focused={focused}) reverse={result}, expected {expect_reverse}; "
            f"spans: {text.spans}"
        )

    def test_render_diff_default_no_reverse(self):
        """render_diff() without focused kwarg defaults to no reverse on title."""
        from claude_visualizer.ui.panels import DIFF_TITLE

        text = render_diff(None)
        assert not _has_reverse_on_title(text, DIFF_TITLE)

    @pytest.mark.parametrize("focused,expect_reverse", [(True, True), (False, False)])
    def test_render_commands_title_style(self, focused, expect_reverse):
        """render_commands focused=True→reverse on title; False→no reverse."""
        from claude_visualizer.ui.panels import COMMANDS_TITLE

        model = CommandFeedModel(AppConfig(command_feed_max=10))
        text = render_commands(model, 0, 80, focused=focused)
        result = _has_reverse_on_title(text, COMMANDS_TITLE)
        assert result == expect_reverse, (
            f"render_commands(focused={focused}) reverse={result}, expected {expect_reverse}; "
            f"spans: {text.spans}"
        )

    def test_render_commands_default_no_reverse(self):
        """render_commands() without focused kwarg defaults to no reverse on title."""
        from claude_visualizer.ui.panels import COMMANDS_TITLE

        model = CommandFeedModel(AppConfig(command_feed_max=10))
        text = render_commands(model, 0, 80)
        assert not _has_reverse_on_title(text, COMMANDS_TITLE)


class TestPageScrollKeys:
    """PageUp/PageDown scroll the focused panel by ~a screenful (_page_step).

    Anti-mock: real run_test + pilot.press.  Each test seeds MORE than one
    screenful of rows so there is guaranteed room to page.
    """

    async def test_mru_pagedown_jumps_more_than_one_row(self, tmp_path: Path):
        """PageDown on focused MRU jumps _scroll_offset by more than 1 row."""
        root = tmp_path / "projects"
        session = root / "proj" / "mru_page.jsonl"
        cfg = _fixture_config(root)
        app = VisualizerApp(cfg)
        async with app.run_test(size=(120, 40)) as pilot:
            # Seed 30 entries so there is more than a screenful to page through.
            for i in range(30):
                _append_line(
                    session,
                    _write_tool_line(
                        "Write",
                        {"file_path": f"/repo/pg_{i:02d}.py", "content": "x"},
                        session_id=f"pgSESS{i:04d}",
                        cwd="/home/dev/pgproj",
                    ),
                )
            await _pump(pilot, "/repo/pg_29.py")

            mru = pilot.app.query_one(MruFilesPanel)
            mru.focus()
            await pilot.pause()

            # One single ↓ step.
            before_down = mru._scroll_offset
            await pilot.press("down")
            await pilot.pause()
            single_step = mru._scroll_offset - before_down
            assert single_step == 1, f"Expected single step=1, got {single_step}"

            # Reset to 0 then measure pagedown jump.
            mru._scroll_offset = 0
            await pilot.press("pagedown")
            await pilot.pause()
            page_jump = mru._scroll_offset
            assert (
                page_jump > 1
            ), f"pagedown must jump by more than 1 row; got _scroll_offset={page_jump}"

    async def test_mru_pageup_returns_toward_zero(self, tmp_path: Path):
        """PageUp on focused MRU decreases _scroll_offset, floored at 0."""
        root = tmp_path / "projects"
        session = root / "proj" / "mru_pageup.jsonl"
        cfg = _fixture_config(root)
        app = VisualizerApp(cfg)
        async with app.run_test(size=(120, 40)) as pilot:
            for i in range(30):
                _append_line(
                    session,
                    _write_tool_line(
                        "Write",
                        {"file_path": f"/repo/pu_{i:02d}.py", "content": "x"},
                        session_id=f"puSESS{i:04d}",
                        cwd="/home/dev/puproj",
                    ),
                )
            await _pump(pilot, "/repo/pu_29.py")

            mru = pilot.app.query_one(MruFilesPanel)
            mru.focus()
            await pilot.pause()

            # Page down first so there is room to page up.
            await pilot.press("pagedown")
            await pilot.pause()
            after_down = mru._scroll_offset
            assert after_down > 0, "pagedown must advance from 0"

            # PageUp must bring offset back toward 0.
            await pilot.press("pageup")
            await pilot.pause()
            after_up = mru._scroll_offset
            assert after_up < after_down, (
                f"pageup must decrease _scroll_offset; after_down={after_down}, "
                f"after_up={after_up}"
            )

            # PageUp at 0 must not go negative.
            mru._scroll_offset = 0
            await pilot.press("pageup")
            await pilot.pause()
            assert mru._scroll_offset == 0, "pageup at offset 0 must stay at 0"

    async def test_commands_pagedown_jumps_more_than_one_row(self, tmp_path: Path):
        """PageDown on focused Commands panel jumps _scroll_offset by more than 1."""
        root = tmp_path / "projects"
        session = root / "proj" / "cmd_page.jsonl"
        cfg = _fixture_config(root)
        app = VisualizerApp(cfg)
        async with app.run_test(size=(120, 40)) as pilot:
            for i in range(30):
                _append_line(
                    session,
                    _write_tool_line(
                        "Bash",
                        {"command": f"cmd-page-{i:02d}"},
                        session_id=f"cmdPG{i:04d}",
                        cwd="/home/dev/cmdpgproj",
                    ),
                )
            await _pump_commands(pilot, "cmd-page-29")

            cmd_panel = pilot.app.query_one(CommandsPanel)
            cmd_panel.focus()
            await pilot.pause()

            # One single ↓ step.
            before_down = cmd_panel._scroll_offset
            await pilot.press("down")
            await pilot.pause()
            single_step = cmd_panel._scroll_offset - before_down
            assert single_step == 1, f"Expected single step=1, got {single_step}"

            # Reset to 0 then measure pagedown jump.
            cmd_panel._scroll_offset = 0
            cmd_panel._follow = False
            await pilot.press("pagedown")
            await pilot.pause()
            page_jump = cmd_panel._scroll_offset
            assert (
                page_jump > 1
            ), f"pagedown must jump more than 1 row; got _scroll_offset={page_jump}"

    async def test_commands_pageup_returns_toward_zero(self, tmp_path: Path):
        """PageUp on focused Commands panel decreases _scroll_offset, floored at 0."""
        root = tmp_path / "projects"
        session = root / "proj" / "cmd_pageup.jsonl"
        cfg = _fixture_config(root)
        app = VisualizerApp(cfg)
        async with app.run_test(size=(120, 40)) as pilot:
            for i in range(30):
                _append_line(
                    session,
                    _write_tool_line(
                        "Bash",
                        {"command": f"cmd-pu-{i:02d}"},
                        session_id=f"cmdPU{i:04d}",
                        cwd="/home/dev/cmdpuproj",
                    ),
                )
            await _pump_commands(pilot, "cmd-pu-29")

            cmd_panel = pilot.app.query_one(CommandsPanel)
            cmd_panel.focus()
            await pilot.pause()

            # Page down first so there is room to page up.
            cmd_panel._scroll_offset = 0
            cmd_panel._follow = False
            await pilot.press("pagedown")
            await pilot.pause()
            after_down = cmd_panel._scroll_offset
            assert after_down > 0, "pagedown must advance from 0"

            # PageUp must bring offset back toward 0.
            await pilot.press("pageup")
            await pilot.pause()
            after_up = cmd_panel._scroll_offset
            assert after_up < after_down, (
                f"pageup must decrease _scroll_offset; after_down={after_down}, "
                f"after_up={after_up}"
            )

            # PageUp at 0 must stay at 0.
            cmd_panel._scroll_offset = 0
            await pilot.press("pageup")
            await pilot.pause()
            assert cmd_panel._scroll_offset == 0, "pageup at offset 0 must stay at 0"

    async def test_diff_pagedown_noop_when_unpinned(self, tmp_path: Path):
        """PageDown on Diff panel does nothing when unpinned (same rule as ↓)."""
        root = tmp_path / "projects"
        session = root / "proj" / "diff_pg_unpin.jsonl"
        clock = _ManualClock()
        cfg = _fixture_config(root)
        app = VisualizerApp(cfg, now=clock)
        async with app.run_test(size=(120, 40)) as pilot:
            tall_content = "\n".join(f"line{i}" for i in range(80))
            _append_line(
                session,
                _write_tool_line(
                    "Write",
                    {"file_path": "/repo/diff_pg_target.py", "content": tall_content},
                    session_id="diffPGSESS01",
                    cwd="/home/dev/diffpgproj",
                ),
            )
            await _pump(pilot, "/repo/diff_pg_target.py")
            await _pump_diff(pilot, "diff_pg_target.py", clock=clock)

            diff = pilot.app.query_one(DiffPanel)
            diff.focus()
            await pilot.pause()

            # Unpinned: PageDown must not change _pin_scroll.
            pin_scroll_before = app._diff_queue._pin_scroll
            await pilot.press("pagedown")
            await pilot.pause()
            assert app._diff_queue._pin_scroll == pin_scroll_before, (
                f"pagedown on UNPINNED diff must not change _pin_scroll; "
                f"before={pin_scroll_before}, after={app._diff_queue._pin_scroll}"
            )

    async def test_diff_pagedown_pages_when_pinned(self, tmp_path: Path):
        """PageDown on pinned Diff panel advances _pin_scroll by more than 1."""
        root = tmp_path / "projects"
        session = root / "proj" / "diff_pg_pin.jsonl"
        clock = _ManualClock()
        cfg = _fixture_config(root)
        app = VisualizerApp(cfg, now=clock)
        async with app.run_test(size=(120, 40)) as pilot:
            tall_content = "\n".join(f"line{i}" for i in range(80))
            _append_line(
                session,
                _write_tool_line(
                    "Write",
                    {"file_path": "/repo/diff_pin_pg.py", "content": tall_content},
                    session_id="diffPinPGSS01",
                    cwd="/home/dev/diffpinproj",
                ),
            )
            await _pump(pilot, "/repo/diff_pin_pg.py")
            await _pump_diff(pilot, "diff_pin_pg.py", clock=clock)

            diff = pilot.app.query_one(DiffPanel)
            diff.focus()

            # Pin via keyboard shortcut.
            await pilot.press("p")
            await pilot.pause()
            assert (
                "📌 pinned" in diff.rendered_text()
            ), "Must be pinned before testing page scroll"

            # Capture pin_scroll after a single ↓ step for comparison.
            pin_before_down = app._diff_queue._pin_scroll
            await pilot.press("down")
            await pilot.pause()
            single_step = app._diff_queue._pin_scroll - pin_before_down
            assert single_step == 1, f"Expected single ↓ step=1, got {single_step}"

            # Reset and measure PageDown jump.
            app._diff_queue._pin_scroll = 0
            await pilot.press("pagedown")
            await pilot.pause()
            page_jump = app._diff_queue._pin_scroll
            assert page_jump > 1, (
                f"pagedown on PINNED diff must advance _pin_scroll by more than 1; "
                f"got {page_jump}"
            )


# ---------------------------------------------------------------------------
# MRU keyboard selection — ↑/↓/PageUp/PageDown posts FileClicked (new feature)
# ---------------------------------------------------------------------------


class _SpyMruPanel(MruFilesPanel):
    """Test-only subclass that records every posted message without side effects.

    Overrides ``post_message`` (the *bus boundary*, not the logic under test)
    to collect messages for assertion, while still calling super() so the
    real dispatch path is preserved.  This is NOT a mock of the SUT; it is a
    thin observation layer on an external-communication seam.
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.posted: list = []

    def post_message(self, message) -> bool:  # type: ignore[override]
        self.posted.append(message)
        return True  # mimic the real return without spinning up an event loop


def _make_mru_panel(prefix: str, count: int) -> tuple:
    """Return a populated (_SpyMruPanel, MruModel) with ``count`` entries."""
    model = MruModel(AppConfig(mru_max=50))
    for i in range(count):
        model.record(
            _file_event(
                file_path=f"/repo/{prefix}_{i:02d}.py",
                session_id=f"{prefix[:6]}{i:04d}abcd",
                project_tag="proj",
            )
        )
    panel = _SpyMruPanel()
    panel.update_from_model(model)
    return panel, model


def _key_event(key: str):
    """Minimal synthetic key event with a no-op stop() for on_key tests."""
    return type("_KeyEvt", (), {"key": key, "stop": lambda self: None})()


class TestMruKeyboardSelect:
    """↑/↓ keys on MruFilesPanel post FileClicked for the row at _scroll_offset."""

    def test_down_key_posts_file_clicked_with_correct_path(self):
        """↓ scrolls by 1 and posts FileClicked for the new _scroll_offset row."""
        panel, model = _make_mru_panel("kbsel", 4)
        assert panel._scroll_offset == 0

        panel.on_key(_key_event("down"))

        assert panel._scroll_offset == 1
        assert len(panel.posted) == 1
        msg = panel.posted[0]
        assert isinstance(msg, MruFilesPanel.FileClicked)
        assert msg.event_key == panel._rows[1].event_key

    def test_up_key_from_non_zero_offset_posts_file_clicked(self):
        """↑ decrements offset and posts FileClicked for the row now at offset."""
        panel, model = _make_mru_panel("kbsel", 4)
        panel._scroll_mru(2)  # advance to offset 2
        panel.posted.clear()

        panel.on_key(_key_event("up"))

        assert panel._scroll_offset == 1
        assert len(panel.posted) == 1
        msg = panel.posted[0]
        assert isinstance(msg, MruFilesPanel.FileClicked)
        assert msg.event_key == panel._rows[1].event_key

    def test_up_key_at_offset_zero_posts_file_clicked_for_row_zero(self):
        """↑ at offset 0 stays at 0 and posts FileClicked for rows()[0]."""
        panel, model = _make_mru_panel("kbsel", 4)
        assert panel._scroll_offset == 0

        panel.on_key(_key_event("up"))

        assert panel._scroll_offset == 0
        assert len(panel.posted) == 1
        assert panel.posted[0].event_key == panel._rows[0].event_key

    def test_empty_panel_down_key_does_not_post_file_clicked(self):
        """↓ on an empty panel (no rows) must NOT post FileClicked."""
        panel = _SpyMruPanel()
        panel.on_key(_key_event("down"))
        assert panel.posted == []


class TestMruKeyboardSelectPageDown:
    """PageDown/PageUp keys post FileClicked for the row at _scroll_offset."""

    def test_pagedown_posts_file_clicked_at_new_offset(self):
        """PageDown scrolls by _page_step() and posts FileClicked."""
        panel, model = _make_mru_panel("pgsel", 10)
        # Force a known page step by placing enough rows and using a non-zero step.
        # We rely on _scroll_mru's clamping so the offset after pagedown is valid.
        panel.on_key(_key_event("pagedown"))

        assert len(panel.posted) == 1
        msg = panel.posted[0]
        assert isinstance(msg, MruFilesPanel.FileClicked)
        assert msg.event_key == panel._rows[panel._scroll_offset].event_key

    def test_pageup_posts_file_clicked_at_new_offset(self):
        """PageUp scrolls back and posts FileClicked for the row at new offset."""
        panel, model = _make_mru_panel("pgsel", 10)
        panel._scroll_mru(5)  # start in the middle
        panel.posted.clear()

        panel.on_key(_key_event("pageup"))

        assert len(panel.posted) == 1
        msg = panel.posted[0]
        assert isinstance(msg, MruFilesPanel.FileClicked)
        assert msg.event_key == panel._rows[panel._scroll_offset].event_key


class TestMruMouseWheelNoSelect:
    """Mouse wheel scroll on MruFilesPanel must NOT post FileClicked (scroll-only)."""

    def test_mouse_scroll_down_does_not_post_file_clicked(self):
        """on_mouse_scroll_down scrolls offset but must NOT post FileClicked."""
        panel, model = _make_mru_panel("wheel", 6)
        panel.on_mouse_scroll_down(None)
        assert (
            panel.posted == []
        ), f"Wheel down must not post FileClicked; got {panel.posted}"
        assert panel._scroll_offset == 1, "Wheel down must still scroll offset"

    def test_mouse_scroll_up_does_not_post_file_clicked(self):
        """on_mouse_scroll_up scrolls offset but must NOT post FileClicked."""
        panel, model = _make_mru_panel("wheel", 6)
        panel._scroll_mru(3)
        panel.posted.clear()

        panel.on_mouse_scroll_up(None)

        assert (
            panel.posted == []
        ), f"Wheel up must not post FileClicked; got {panel.posted}"
        assert panel._scroll_offset == 2, "Wheel up must still scroll offset"


class TestTitleHighlightIntegration:
    """Integration: on_focus/on_blur re-render causes title highlight to follow focus."""

    async def test_focus_mru_highlights_mru_title_only(self, tmp_path: Path):
        """MRU title is 'reverse' when focused; Diff and Commands titles are plain."""
        cfg = _fixture_config(tmp_path / "projects")
        app = VisualizerApp(cfg)
        from claude_visualizer.ui.panels import COMMANDS_TITLE, DIFF_TITLE, MRU_TITLE

        async with app.run_test(size=(120, 40)) as pilot:
            pilot.app.query_one(MruFilesPanel).focus()
            await pilot.pause()
            await pilot.pause()

            assert _has_reverse_on_title(
                pilot.app.query_one(MruFilesPanel)._renderable, MRU_TITLE
            ), "MRU title must be 'reverse' when focused"
            assert not _has_reverse_on_title(
                pilot.app.query_one(DiffPanel)._renderable, DIFF_TITLE
            ), "Diff title must NOT be 'reverse' when MRU is focused"
            assert not _has_reverse_on_title(
                pilot.app.query_one(CommandsPanel)._renderable, COMMANDS_TITLE
            ), "Commands title must NOT be 'reverse' when MRU is focused"

    async def test_tab_moves_highlight_from_mru_to_diff(self, tmp_path: Path):
        """After Tab from MRU → Diff, Diff title gets 'reverse' and MRU loses it."""
        cfg = _fixture_config(tmp_path / "projects")
        app = VisualizerApp(cfg)
        from claude_visualizer.ui.panels import DIFF_TITLE, MRU_TITLE

        async with app.run_test(size=(120, 40)) as pilot:
            pilot.app.query_one(MruFilesPanel).focus()
            await pilot.pause()
            await pilot.press("tab")
            await pilot.pause()
            await pilot.pause()

            assert _has_reverse_on_title(
                pilot.app.query_one(DiffPanel)._renderable, DIFF_TITLE
            ), "Diff title must be 'reverse' after Tab from MRU"
            assert not _has_reverse_on_title(
                pilot.app.query_one(MruFilesPanel)._renderable, MRU_TITLE
            ), "MRU title must NOT be 'reverse' after focus moved to Diff"


# ---------------------------------------------------------------------------
# Home / End key scrolling
# ---------------------------------------------------------------------------


class TestHomeEndKeys:
    """Home/End scroll the focused panel to the very top/bottom.

    Anti-mock: real run_test + pilot.press.  Each test seeds MORE than one
    screenful of rows so there is guaranteed room to jump.
    """

    async def test_mru_home_scrolls_to_top(self, tmp_path: Path):
        """Home on focused MRU resets _scroll_offset to 0."""
        root = tmp_path / "projects"
        session = root / "proj" / "mru_home.jsonl"
        cfg = _fixture_config(root)
        app = VisualizerApp(cfg)
        async with app.run_test(size=(120, 40)) as pilot:
            for i in range(30):
                _append_line(
                    session,
                    _write_tool_line(
                        "Write",
                        {"file_path": f"/repo/he_{i:02d}.py", "content": "x"},
                        session_id=f"heSESS{i:04d}",
                        cwd="/home/dev/heproj",
                    ),
                )
            await _pump(pilot, "/repo/he_29.py")

            mru = pilot.app.query_one(MruFilesPanel)
            mru.focus()
            await pilot.pause()

            # Scroll down first so offset > 0.
            mru._scroll_offset = 15
            await pilot.press("home")
            await pilot.pause()
            assert (
                mru._scroll_offset == 0
            ), f"home must reset _scroll_offset to 0; got {mru._scroll_offset}"

    async def test_mru_end_scrolls_to_bottom(self, tmp_path: Path):
        """End on focused MRU sets _scroll_offset to len(rows) - 1."""
        root = tmp_path / "projects"
        session = root / "proj" / "mru_end.jsonl"
        cfg = _fixture_config(root)
        app = VisualizerApp(cfg)
        async with app.run_test(size=(120, 40)) as pilot:
            for i in range(30):
                _append_line(
                    session,
                    _write_tool_line(
                        "Write",
                        {"file_path": f"/repo/en_{i:02d}.py", "content": "x"},
                        session_id=f"enSESS{i:04d}",
                        cwd="/home/dev/enproj",
                    ),
                )
            await _pump(pilot, "/repo/en_29.py")

            mru = pilot.app.query_one(MruFilesPanel)
            mru.focus()
            await pilot.pause()

            # Ensure offset starts at 0.
            mru._scroll_offset = 0
            await pilot.press("end")
            await pilot.pause()
            expected_max = len(mru._rows) - 1
            assert mru._scroll_offset == expected_max, (
                f"end must set _scroll_offset to {expected_max}; "
                f"got {mru._scroll_offset}"
            )

    async def test_commands_home_scrolls_to_top(self, tmp_path: Path):
        """Home on focused Commands panel resets _scroll_offset to 0 and re-arms follow."""
        root = tmp_path / "projects"
        session = root / "proj" / "cmd_home.jsonl"
        cfg = _fixture_config(root)
        app = VisualizerApp(cfg)
        async with app.run_test(size=(120, 40)) as pilot:
            for i in range(30):
                _append_line(
                    session,
                    _write_tool_line(
                        "Bash",
                        {"command": f"cmd-home-{i:02d}"},
                        session_id=f"cmdHM{i:04d}",
                        cwd="/home/dev/cmdhomproj",
                    ),
                )
            await _pump_commands(pilot, "cmd-home-29")

            cmd_panel = pilot.app.query_one(CommandsPanel)
            cmd_panel.focus()
            await pilot.pause()

            # Scroll away from top first.
            cmd_panel._scroll_offset = 15
            cmd_panel._follow = False
            await pilot.press("home")
            await pilot.pause()
            assert (
                cmd_panel._scroll_offset == 0
            ), f"home must reset _scroll_offset to 0; got {cmd_panel._scroll_offset}"
            assert cmd_panel._follow is True, "home must re-arm autoscroll follow"

    async def test_commands_end_scrolls_to_bottom(self, tmp_path: Path):
        """End on focused Commands panel sets _scroll_offset to len(rows) - 1."""
        root = tmp_path / "projects"
        session = root / "proj" / "cmd_end.jsonl"
        cfg = _fixture_config(root)
        app = VisualizerApp(cfg)
        async with app.run_test(size=(120, 40)) as pilot:
            for i in range(30):
                _append_line(
                    session,
                    _write_tool_line(
                        "Bash",
                        {"command": f"cmd-end-{i:02d}"},
                        session_id=f"cmdEN{i:04d}",
                        cwd="/home/dev/cmdendproj",
                    ),
                )
            await _pump_commands(pilot, "cmd-end-29")

            cmd_panel = pilot.app.query_one(CommandsPanel)
            cmd_panel.focus()
            await pilot.pause()

            # Start at top.
            cmd_panel._scroll_offset = 0
            cmd_panel._follow = False
            await pilot.press("end")
            await pilot.pause()
            expected_max = len(cmd_panel._last_model.rows()) - 1
            assert cmd_panel._scroll_offset == expected_max, (
                f"end must set _scroll_offset to {expected_max}; "
                f"got {cmd_panel._scroll_offset}"
            )

    async def test_diff_home_noop_when_unpinned(self, tmp_path: Path):
        """Home on unpinned Diff panel does not change _pin_scroll."""
        root = tmp_path / "projects"
        session = root / "proj" / "diff_home_unpin.jsonl"
        clock = _ManualClock()
        cfg = _fixture_config(root)
        app = VisualizerApp(cfg, now=clock)
        async with app.run_test(size=(120, 40)) as pilot:
            tall_content = "\n".join(f"line{i}" for i in range(80))
            _append_line(
                session,
                _write_tool_line(
                    "Write",
                    {"file_path": "/repo/diff_home_target.py", "content": tall_content},
                    session_id="diffHMSS01",
                    cwd="/home/dev/diffhmproj",
                ),
            )
            await _pump(pilot, "/repo/diff_home_target.py")
            await _pump_diff(pilot, "diff_home_target.py", clock=clock)

            diff = pilot.app.query_one(DiffPanel)
            diff.focus()
            await pilot.pause()

            pin_before = app._diff_queue._pin_scroll
            await pilot.press("home")
            await pilot.pause()
            assert app._diff_queue._pin_scroll == pin_before, (
                f"home on UNPINNED diff must not change _pin_scroll; "
                f"before={pin_before}, after={app._diff_queue._pin_scroll}"
            )

    async def test_diff_end_noop_when_unpinned(self, tmp_path: Path):
        """End on unpinned Diff panel does not change _pin_scroll."""
        root = tmp_path / "projects"
        session = root / "proj" / "diff_end_unpin.jsonl"
        clock = _ManualClock()
        cfg = _fixture_config(root)
        app = VisualizerApp(cfg, now=clock)
        async with app.run_test(size=(120, 40)) as pilot:
            tall_content = "\n".join(f"line{i}" for i in range(80))
            _append_line(
                session,
                _write_tool_line(
                    "Write",
                    {"file_path": "/repo/diff_end_target.py", "content": tall_content},
                    session_id="diffENSS01",
                    cwd="/home/dev/diffenproj",
                ),
            )
            await _pump(pilot, "/repo/diff_end_target.py")
            await _pump_diff(pilot, "diff_end_target.py", clock=clock)

            diff = pilot.app.query_one(DiffPanel)
            diff.focus()
            await pilot.pause()

            pin_before = app._diff_queue._pin_scroll
            await pilot.press("end")
            await pilot.pause()
            assert app._diff_queue._pin_scroll == pin_before, (
                f"end on UNPINNED diff must not change _pin_scroll; "
                f"before={pin_before}, after={app._diff_queue._pin_scroll}"
            )

    async def test_diff_home_jumps_to_zero_when_pinned(self, tmp_path: Path):
        """Home on pinned Diff panel sets _pin_scroll to 0."""
        root = tmp_path / "projects"
        session = root / "proj" / "diff_home_pin.jsonl"
        clock = _ManualClock()
        cfg = _fixture_config(root)
        app = VisualizerApp(cfg, now=clock)
        async with app.run_test(size=(120, 40)) as pilot:
            tall_content = "\n".join(f"line{i}" for i in range(80))
            _append_line(
                session,
                _write_tool_line(
                    "Write",
                    {"file_path": "/repo/diff_home_pin.py", "content": tall_content},
                    session_id="diffHPSS01",
                    cwd="/home/dev/diffhpproj",
                ),
            )
            await _pump(pilot, "/repo/diff_home_pin.py")
            await _pump_diff(pilot, "diff_home_pin.py", clock=clock)

            diff = pilot.app.query_one(DiffPanel)
            diff.focus()
            await pilot.press("p")
            await pilot.pause()
            assert "📌 pinned" in diff.rendered_text(), "Must be pinned before test"

            # Advance scroll position so Home has somewhere to go.
            app._diff_queue._pin_scroll = 10
            await pilot.press("home")
            await pilot.pause()
            assert app._diff_queue._pin_scroll == 0, (
                f"home on PINNED diff must set _pin_scroll to 0; "
                f"got {app._diff_queue._pin_scroll}"
            )

    async def test_diff_end_jumps_to_max_when_pinned(self, tmp_path: Path):
        """End on pinned Diff panel sets _pin_scroll to max_scroll."""
        root = tmp_path / "projects"
        session = root / "proj" / "diff_end_pin.jsonl"
        clock = _ManualClock()
        cfg = _fixture_config(root)
        app = VisualizerApp(cfg, now=clock)
        async with app.run_test(size=(120, 40)) as pilot:
            tall_content = "\n".join(f"line{i}" for i in range(80))
            _append_line(
                session,
                _write_tool_line(
                    "Write",
                    {"file_path": "/repo/diff_end_pin.py", "content": tall_content},
                    session_id="diffEPSS01",
                    cwd="/home/dev/diffepproj",
                ),
            )
            await _pump(pilot, "/repo/diff_end_pin.py")
            await _pump_diff(pilot, "diff_end_pin.py", clock=clock)

            diff = pilot.app.query_one(DiffPanel)
            diff.focus()
            await pilot.press("p")
            await pilot.pause()
            assert "📌 pinned" in diff.rendered_text(), "Must be pinned before test"

            # Start at top and press End.
            app._diff_queue._pin_scroll = 0
            await pilot.press("end")
            await pilot.pause()
            assert app._diff_queue._pin_scroll > 0, (
                f"end on PINNED diff must advance _pin_scroll beyond 0; "
                f"got {app._diff_queue._pin_scroll}"
            )
