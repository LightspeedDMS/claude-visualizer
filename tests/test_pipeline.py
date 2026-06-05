"""Tests for pipeline.py — async orchestration of discovery + tail + parse.

Anti-mock: every test drives a REAL temporary ``projects_root`` directory,
appends REAL JSONL lines to REAL files, and asserts the resulting events reach
the consumer through the bounded ``asyncio.Queue``.  No filesystem mocking, no
patched clocks — the pipeline runs in a live asyncio loop exactly as production.

The pipeline is the producer: it discovers active transcripts, tails them
incrementally, parses complete lines, and puts ``Event`` objects on its queue.
Routing of ``FileModifiedEvent`` → ``MruModel`` is a separate pure helper
(:func:`route_event`) used by both these tests and the Textual UI.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

from claude_visualizer.config import AppConfig
from claude_visualizer.events import CommandEvent, FileModifiedEvent, FileOp
from claude_visualizer.models.mru import MruModel
from claude_visualizer.pipeline import Pipeline, route_event
from claude_visualizer.ui.panels import SUBAGENT_MARKER, render_mru

# Real on-disk fixtures shipped with the test suite.
FIXTURE_DIR = Path(__file__).parent / "fixtures"
SUBAGENT_FIXTURE = FIXTURE_DIR / "session_subagent.jsonl"
THINKING_FIXTURE = FIXTURE_DIR / "session_thinking.jsonl"


# ---------------------------------------------------------------------------
# Helpers — build real JSONL transcript lines and append to real files
# ---------------------------------------------------------------------------


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
    """Fast-polling config rooted at a fixture dir for snappy tests."""
    base = dict(
        projects_root=root,
        active_window_seconds=3600,  # everything in the test window is "active"
        discovery_interval_seconds=0.05,
        poll_interval_seconds=0.05,
        seed_tail_bytes=65_536,
        max_line_bytes=1_000_000,
        mru_max=50,
    )
    base.update(overrides)
    return AppConfig(**base)


async def _next_file_event(
    pipeline: Pipeline, timeout: float = 5.0
) -> FileModifiedEvent:
    """Await the next FileModifiedEvent off the pipeline queue (bounded wait)."""
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            raise AssertionError("no FileModifiedEvent within timeout")
        evt = await asyncio.wait_for(pipeline.get_event(), timeout=remaining)
        if isinstance(evt, FileModifiedEvent):
            return evt


# ---------------------------------------------------------------------------
# route_event — pure dispatch helper (no asyncio, no IO)
# ---------------------------------------------------------------------------


class TestRouteEvent:
    def test_file_modified_event_recorded_into_mru(self):
        cfg = AppConfig(mru_max=10)
        model = MruModel(cfg)
        evt = FileModifiedEvent(
            ts=None,
            session_id="sess1234abcd",
            is_subagent=False,
            project_tag="proj",
            source_path="/x/a.jsonl",
            file_path="/repo/main.py",
            op=FileOp.WRITE,
        )
        route_event(evt, model)
        rows = model.rows()
        assert len(rows) == 1
        assert rows[0].file_path == "/repo/main.py"

    def test_command_event_not_recorded_into_mru(self):
        cfg = AppConfig(mru_max=10)
        model = MruModel(cfg)
        evt = CommandEvent(
            ts=None,
            session_id="sess1234abcd",
            is_subagent=False,
            project_tag="proj",
            source_path="/x/a.jsonl",
            command="ls -la",
        )
        route_event(evt, model)
        assert model.rows() == []


class TestRouteEventDiffQueue:
    """``route_event`` also feeds the diff display queue (story #3)."""

    def test_file_modified_event_recorded_into_diff_queue(self):
        from claude_visualizer.models.diff_queue import DiffQueueModel

        cfg = AppConfig(mru_max=10)
        mru = MruModel(cfg)
        diff_queue = DiffQueueModel(cfg, now=lambda: 0.0)
        evt = FileModifiedEvent(
            ts=None,
            session_id="sess1234abcd",
            is_subagent=False,
            project_tag="proj",
            source_path="/x/a.jsonl",
            file_path="/repo/main.py",
            op=FileOp.WRITE,
            full_content="print('hi')",
            model="claude-opus-4-8",
        )
        route_event(evt, mru, diff_queue)
        state = diff_queue.tick(0.0, viewport_height=10)
        assert state is not None
        assert state.file_path == "/repo/main.py"

    def test_command_event_not_recorded_into_diff_queue(self):
        from claude_visualizer.models.diff_queue import DiffQueueModel

        cfg = AppConfig(mru_max=10)
        mru = MruModel(cfg)
        diff_queue = DiffQueueModel(cfg, now=lambda: 0.0)
        evt = CommandEvent(
            ts=None,
            session_id="sess1234abcd",
            is_subagent=False,
            project_tag="proj",
            source_path="/x/a.jsonl",
            command="ls -la",
        )
        route_event(evt, mru, diff_queue)
        # Nothing recorded → tick rests on empty (file_path is None).
        state = diff_queue.tick(0.0, viewport_height=10)
        assert state is not None
        assert state.file_path is None

    def test_diff_queue_is_optional_for_mru_only_callers(self):
        # Existing call sites pass only the MRU model; that must keep working.
        cfg = AppConfig(mru_max=10)
        mru = MruModel(cfg)
        evt = FileModifiedEvent(
            ts=None,
            session_id="sess1234abcd",
            is_subagent=False,
            project_tag="proj",
            source_path="/x/a.jsonl",
            file_path="/repo/only_mru.py",
            op=FileOp.WRITE,
        )
        route_event(evt, mru)  # no diff_queue argument
        assert mru.rows()[0].file_path == "/repo/only_mru.py"


class TestRouteEventCommandFeed:
    """``route_event`` feeds the bottom Commands feed model (story #4)."""

    def test_command_event_recorded_into_command_feed(self):
        from claude_visualizer.models.command_feed import CommandFeedModel

        cfg = AppConfig(mru_max=10, command_feed_max=10)
        mru = MruModel(cfg)
        feed = CommandFeedModel(cfg)
        evt = CommandEvent(
            ts=None,
            session_id="sess1234abcd",
            is_subagent=False,
            project_tag="proj",
            source_path="/x/a.jsonl",
            command="pytest -q",
        )
        route_event(evt, mru, command_feed=feed)
        rows = feed.rows()
        assert len(rows) == 1
        assert rows[0].command == "pytest -q"

    def test_file_modified_event_not_recorded_into_command_feed(self):
        from claude_visualizer.models.command_feed import CommandFeedModel

        cfg = AppConfig(mru_max=10, command_feed_max=10)
        mru = MruModel(cfg)
        feed = CommandFeedModel(cfg)
        evt = FileModifiedEvent(
            ts=None,
            session_id="sess1234abcd",
            is_subagent=False,
            project_tag="proj",
            source_path="/x/a.jsonl",
            file_path="/repo/main.py",
            op=FileOp.WRITE,
        )
        route_event(evt, mru, command_feed=feed)
        assert feed.rows() == []

    def test_command_feed_is_optional_for_existing_callers(self):
        # MRU-only / diff-only callers must keep working without a feed model.
        cfg = AppConfig(mru_max=10)
        mru = MruModel(cfg)
        evt = CommandEvent(
            ts=None,
            session_id="sess1234abcd",
            is_subagent=False,
            project_tag="proj",
            source_path="/x/a.jsonl",
            command="ls -la",
        )
        # No command_feed argument → no error, command simply not fed anywhere.
        route_event(evt, mru)
        assert mru.rows() == []

    def test_two_commands_recorded_newest_on_top(self):
        from claude_visualizer.models.command_feed import CommandFeedModel

        cfg = AppConfig(mru_max=10, command_feed_max=10)
        mru = MruModel(cfg)
        feed = CommandFeedModel(cfg)
        for c in ("first", "second"):
            route_event(
                CommandEvent(
                    ts=None,
                    session_id="sess1234abcd",
                    is_subagent=False,
                    project_tag="proj",
                    source_path="/x/a.jsonl",
                    command=c,
                ),
                mru,
                command_feed=feed,
            )
        assert [r.command for r in feed.rows()] == ["second", "first"]


# ---------------------------------------------------------------------------
# Pipeline lifecycle and live tailing (AC2, AC8, AC10)
# ---------------------------------------------------------------------------


class TestPipelineLiveTailing:
    async def test_appended_write_line_surfaces_as_event(self, tmp_path: Path):
        root = tmp_path / "projects"
        session = root / "proj" / "session-abc.jsonl"
        # Seed an existing (active) file so it is in the active set at startup.
        _append_line(
            session,
            _write_tool_line("Write", {"file_path": "/repo/seed.py", "content": "x"}),
        )
        cfg = _fixture_config(root)
        pipeline = Pipeline(cfg)
        await pipeline.start()
        try:
            # Append a NEW write after the pipeline is tailing.
            _append_line(
                session,
                _write_tool_line(
                    "Write", {"file_path": "/repo/live.py", "content": "y"}
                ),
            )
            paths_seen = set()
            for _ in range(5):
                evt = await _next_file_event(pipeline)
                paths_seen.add(evt.file_path)
                if "/repo/live.py" in paths_seen:
                    break
            assert "/repo/live.py" in paths_seen
        finally:
            await pipeline.stop()

    async def test_event_carries_origin_metadata(self, tmp_path: Path):
        root = tmp_path / "projects"
        session = root / "proj" / "session-meta.jsonl"
        _append_line(
            session, _write_tool_line("Write", {"file_path": "/r/s.py", "content": "x"})
        )
        cfg = _fixture_config(root)
        pipeline = Pipeline(cfg)
        await pipeline.start()
        try:
            _append_line(
                session,
                _write_tool_line(
                    "Edit",
                    {"file_path": "/r/edited.py", "old_string": "a", "new_string": "b"},
                    session_id="origin999xyz",
                    cwd="/home/dev/cool-project",
                ),
            )
            evt = None
            for _ in range(6):
                candidate = await _next_file_event(pipeline)
                if candidate.file_path == "/r/edited.py":
                    evt = candidate
                    break
            assert evt is not None
            assert evt.project_tag == "cool-project"
            assert evt.session_id == "origin999xyz"
            assert evt.op == FileOp.EDIT
        finally:
            await pipeline.stop()


# ---------------------------------------------------------------------------
# AC7: new session transcript appearing mid-run is discovered and tailed
# ---------------------------------------------------------------------------


class TestPipelineNewSessionMidRun:
    async def test_new_session_file_discovered_after_start(self, tmp_path: Path):
        root = tmp_path / "projects"
        # Start with one existing session so the tree is non-empty.
        existing = root / "proj" / "existing.jsonl"
        _append_line(
            existing,
            _write_tool_line("Write", {"file_path": "/r/old.py", "content": "x"}),
        )
        cfg = _fixture_config(root)
        pipeline = Pipeline(cfg)
        await pipeline.start()
        try:
            # A brand-new session file appears AFTER startup (AC7).
            newfile = root / "proj2" / "brand-new-session.jsonl"
            _append_line(
                newfile,
                _write_tool_line(
                    "Write",
                    {"file_path": "/r/fresh.py", "content": "z"},
                    session_id="newsess000111",
                ),
            )
            found = False
            for _ in range(8):
                evt = await _next_file_event(pipeline)
                if evt.file_path == "/r/fresh.py":
                    found = True
                    break
            assert found, "newly-created mid-run session was not discovered/tailed"
        finally:
            await pipeline.stop()

    async def test_subagent_transcript_surfaces_with_flag(self, tmp_path: Path):
        root = tmp_path / "projects"
        sub = root / "proj" / "sess" / "subagents" / "agent-zzz.jsonl"
        cfg = _fixture_config(root)
        pipeline = Pipeline(cfg)
        await pipeline.start()
        try:
            _append_line(
                sub,
                _write_tool_line(
                    "Write",
                    {"file_path": "/r/sub.py", "content": "s"},
                    session_id="subAGENT0001",
                ),
            )
            evt = None
            for _ in range(8):
                candidate = await _next_file_event(pipeline)
                if candidate.file_path == "/r/sub.py":
                    evt = candidate
                    break
            assert evt is not None
            assert evt.is_subagent is True
        finally:
            await pipeline.stop()


# ---------------------------------------------------------------------------
# Clean shutdown (AC9 teardown contract at the pipeline layer)
# ---------------------------------------------------------------------------


class TestPipelineShutdown:
    async def test_stop_cancels_tasks_and_is_idempotent(self, tmp_path: Path):
        root = tmp_path / "projects"
        _append_line(
            root / "p" / "s.jsonl",
            _write_tool_line("Write", {"file_path": "/r/x.py", "content": "x"}),
        )
        cfg = _fixture_config(root)
        pipeline = Pipeline(cfg)
        await pipeline.start()
        await pipeline.stop()
        assert pipeline.is_running() is False
        # Second stop must be a harmless no-op (idempotent teardown).
        await pipeline.stop()
        assert pipeline.is_running() is False

    async def test_double_start_is_noop(self, tmp_path: Path):
        root = tmp_path / "projects"
        _append_line(
            root / "p" / "s.jsonl",
            _write_tool_line("Write", {"file_path": "/r/x.py", "content": "x"}),
        )
        cfg = _fixture_config(root)
        pipeline = Pipeline(cfg)
        await pipeline.start()
        first_tasks = list(pipeline._tasks)
        await pipeline.start()  # second start must not spawn new tasks
        try:
            assert pipeline._tasks == first_tasks
            assert pipeline.is_running() is True
        finally:
            await pipeline.stop()


# ---------------------------------------------------------------------------
# Active-set reconciliation and bounded queue internals
# ---------------------------------------------------------------------------


class TestPipelineActiveSetReconcile:
    async def test_tailer_dropped_when_file_ages_out(self, tmp_path: Path):
        # Window is tiny: a file whose mtime is pushed into the past must be
        # dropped from the tailer dict on the next active-set refresh.
        root = tmp_path / "projects"
        f = root / "p" / "aging.jsonl"
        _append_line(
            f, _write_tool_line("Write", {"file_path": "/r/x.py", "content": "x"})
        )
        cfg = _fixture_config(root, active_window_seconds=60)
        pipeline = Pipeline(cfg)
        pipeline._refresh_active_set()
        assert str(f.resolve()) in pipeline._tails
        # Age the file far outside the window, then re-refresh.
        old = asyncio.get_event_loop().time()  # noqa: F841 (clarity only)
        import os as _os
        import time as _time

        past = _time.time() - 10_000
        _os.utime(f, (past, past))
        pipeline._refresh_active_set()
        assert str(f.resolve()) not in pipeline._tails

    def test_enqueue_discards_oldest_when_saturated(self, tmp_path: Path):
        # White-box: shrink the queue to capacity 2, enqueue 3 events, and
        # assert the oldest was discarded (bounded-memory back-pressure).
        root = tmp_path / "projects"
        cfg = _fixture_config(root)
        pipeline = Pipeline(cfg)
        pipeline._queue = asyncio.Queue(maxsize=2)

        def _evt(path: str) -> FileModifiedEvent:
            return FileModifiedEvent(
                ts=None,
                session_id="s",
                is_subagent=False,
                project_tag="p",
                source_path="/x.jsonl",
                file_path=path,
                op=FileOp.WRITE,
            )

        pipeline._enqueue(_evt("/a"))
        pipeline._enqueue(_evt("/b"))
        pipeline._enqueue(_evt("/c"))  # overflow → "/a" discarded
        drained = [pipeline._queue.get_nowait().file_path for _ in range(2)]
        assert drained == ["/b", "/c"]
        assert pipeline._queue.empty()

    async def test_root_created_after_start_is_tolerated(self, tmp_path: Path):
        # projects_root does NOT exist when the pipeline starts (a fresh
        # machine where ~/.claude/projects has not been created yet).  The
        # watch loop must wait for it rather than crash, then surface events
        # once the root and a session file appear.
        root = tmp_path / "not-yet" / "projects"
        assert not root.exists()
        cfg = _fixture_config(root)
        pipeline = Pipeline(cfg)
        await pipeline.start()
        try:
            # Create the root + a session file AFTER startup.
            session = root / "proj" / "late.jsonl"
            _append_line(
                session,
                _write_tool_line("Write", {"file_path": "/r/late.py", "content": "L"}),
            )
            found = False
            for _ in range(10):
                evt = await _next_file_event(pipeline)
                if evt.file_path == "/r/late.py":
                    found = True
                    break
            assert found, "events did not surface after late root creation"
        finally:
            await pipeline.stop()

    async def test_root_removed_midrun_is_tolerated(self, tmp_path: Path):
        # The root exists and is being watched, then it is removed entirely
        # (e.g. user wipes ~/.claude).  The watcher must not crash the task;
        # when a fresh root + file reappears, tailing resumes.
        import shutil

        root = tmp_path / "projects"
        _append_line(
            root / "proj" / "a.jsonl",
            _write_tool_line("Write", {"file_path": "/r/a.py", "content": "x"}),
        )
        cfg = _fixture_config(root)
        pipeline = Pipeline(cfg)
        await pipeline.start()
        try:
            # Drain the seeded event so the queue is clear.
            await _next_file_event(pipeline)
            # Nuke the whole root mid-watch.
            shutil.rmtree(root)
            await asyncio.sleep(0.2)  # let the watcher observe the removal
            assert pipeline.is_running() is True  # task survived the removal
            # Recreate the root with a brand-new file; tailing must resume.
            _append_line(
                root / "proj2" / "b.jsonl",
                _write_tool_line("Write", {"file_path": "/r/b.py", "content": "y"}),
            )
            found = False
            for _ in range(15):
                evt = await _next_file_event(pipeline)
                if evt.file_path == "/r/b.py":
                    found = True
                    break
            assert found, "tailing did not resume after root was recreated"
        finally:
            await pipeline.stop()

    async def test_discovery_loop_picks_up_second_new_file(self, tmp_path: Path):
        # Exercises the periodic re-scan path: after the first new file is
        # seen, a SECOND new file created later must also be discovered.
        root = tmp_path / "projects"
        _append_line(
            root / "p" / "first.jsonl",
            _write_tool_line("Write", {"file_path": "/r/one.py", "content": "x"}),
        )
        cfg = _fixture_config(root)
        pipeline = Pipeline(cfg)
        await pipeline.start()
        try:
            second = root / "p" / "second.jsonl"
            _append_line(
                second,
                _write_tool_line("Write", {"file_path": "/r/two.py", "content": "y"}),
            )
            seen = set()
            for _ in range(12):
                evt = await _next_file_event(pipeline)
                seen.add(evt.file_path)
                if "/r/two.py" in seen:
                    break
            assert "/r/two.py" in seen
        finally:
            await pipeline.stop()


# ---------------------------------------------------------------------------
# Subagent fixture integration (AC5 — wires the real session_subagent.jsonl
# fixture through discovery → tail → parse → route → render end-to-end so the
# subagent origin is detected and the ⤷sub marker reaches the rendered panel).
# ---------------------------------------------------------------------------


class TestPipelineThinkingCorrelation:
    """AC4 end-to-end: requestId→thinking correlation survives the live stream.

    The real ``session_thinking.jsonl`` fixture has a thinking block in one
    entry and the Write tool_use in a SEPARATE later entry sharing the same
    ``requestId``.  Correlation is therefore cross-entry, which only works if
    the pipeline holds ONE long-lived :class:`EventExtractor` (not a fresh
    per-line parse).  Real bytes, copied verbatim — no synthesised content.
    """

    async def test_write_after_thinking_flagged_through_pipeline(self, tmp_path: Path):
        assert THINKING_FIXTURE.is_file()
        root = tmp_path / "projects"
        # Prime an unrelated active file so the active set exists at startup,
        # then drop the thinking fixture in as a brand-new transcript mid-run.
        _append_line(
            root / "think-project" / "primer.jsonl",
            _write_tool_line("Write", {"file_path": "/r/primer.py", "content": "x"}),
        )
        cfg = _fixture_config(root)
        pipeline = Pipeline(cfg)
        await pipeline.start()
        try:
            dest = root / "think-project" / "thinksess001.jsonl"
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(THINKING_FIXTURE, dest)

            write_evt = None
            edit_evt = None
            for _ in range(60):
                evt = await _next_file_event(pipeline)
                if evt.file_path != "/home/user/think-project/app.py":
                    continue
                if evt.op == FileOp.WRITE:
                    write_evt = evt
                elif evt.op == FileOp.EDIT:
                    edit_evt = evt
                if write_evt is not None and edit_evt is not None:
                    break

            assert write_evt is not None, "thinking-correlated Write never surfaced"
            assert write_evt.used_thinking is True
            assert write_evt.thinking_chars > 0

            assert edit_evt is not None, "non-thinking Edit never surfaced"
            assert edit_evt.used_thinking is False
            assert edit_evt.thinking_chars == 0
        finally:
            await pipeline.stop()


class TestSubagentFixtureIntegration:
    """Drive the REAL ``session_subagent.jsonl`` fixture through the pipeline.

    The fixture is a genuine subagent transcript (one ``Write`` tool_use).
    Subagent detection is path-based (``.../subagents/agent-*.jsonl``), so the
    fixture bytes are copied verbatim into a real ``projects_root`` at the
    canonical subagent path, then the live pipeline surfaces the event.  We
    assert the origin flag end-to-end AND that the marker renders.
    """

    # The file_path written by the Write tool_use inside the fixture.
    _FIXTURE_FILE = "/home/user/my-project/lib/helper.py"

    def _install_fixture(self, root: Path) -> Path:
        """Copy the real fixture into a canonical subagent path under ``root``."""
        dest = (
            root
            / "my-project"
            / "session-sub999aaa"
            / "subagents"
            / "agent-deadbeef.jsonl"
        )
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Real bytes, copied verbatim — no synthesised content (anti-mock).
        shutil.copyfile(SUBAGENT_FIXTURE, dest)
        return dest

    async def test_subagent_fixture_event_flagged_and_marker_rendered(
        self, tmp_path: Path
    ):
        # Sanity: the orphaned fixture actually exists on disk.
        assert SUBAGENT_FIXTURE.is_file()

        root = tmp_path / "projects"
        # Seed an unrelated active file so the active set is primed at startup,
        # then drop the fixture in as a brand-new subagent transcript mid-run.
        _append_line(
            root / "my-project" / "session-main.jsonl",
            _write_tool_line("Write", {"file_path": "/r/primer.py", "content": "x"}),
        )
        cfg = _fixture_config(root)
        pipeline = Pipeline(cfg)
        await pipeline.start()
        try:
            self._install_fixture(root)

            # Drain events until the fixture's helper.py surfaces.
            subagent_evt = None
            for _ in range(40):
                evt = await _next_file_event(pipeline)
                if evt.file_path == self._FIXTURE_FILE:
                    subagent_evt = evt
                    break
            assert subagent_evt is not None, "subagent fixture event never surfaced"

            # End-to-end origin detection from the real fixture's path.
            assert subagent_evt.is_subagent is True
            # The fixture's assistant line carries no ``cwd`` field, so the
            # parser correctly derives an empty project tag (basename of "").
            assert subagent_evt.project_tag == ""

            # Route into the MRU model and render: the ⤷sub marker must appear
            # on the rendered row for the subagent-originated file.
            model = MruModel(cfg)
            route_event(subagent_evt, model)
            # render_mru now returns a Rich Text (colours the highlighted row);
            # read its plain string for line-level assertions.
            rendered = render_mru(model).plain
            assert self._FIXTURE_FILE in rendered
            assert SUBAGENT_MARKER in rendered
            # The marker is on the SAME row as the subagent file (not a stray).
            sub_rows = [ln for ln in rendered.splitlines() if self._FIXTURE_FILE in ln]
            assert sub_rows and SUBAGENT_MARKER in sub_rows[0]
        finally:
            await pipeline.stop()
