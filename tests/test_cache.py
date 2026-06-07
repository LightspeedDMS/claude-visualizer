"""Tests for ``CacheDB`` — SQLite-backed persistence for file and command events.

All tests use real temp files (real CacheDB instances on real SQLite DBs).
Anti-mock: no mocks anywhere.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from claude_visualizer.cache import CacheDB
from claude_visualizer.config import AppConfig
from claude_visualizer.events import CommandEvent, FileModifiedEvent, FileOp
from claude_visualizer.ui.app import VisualizerApp

TS = datetime(2024, 3, 15, 12, 0, 0, tzinfo=timezone.utc)


def _file_event(
    file_path: str = "/repo/foo.py",
    op: FileOp = FileOp.EDIT,
    session_id: str = "sess1234abcd",
    project_tag: str = "myproj",
    is_subagent: bool = False,
    used_thinking: bool = False,
    thinking_chars: int = 0,
    ts: datetime | None = TS,
    model: str = "claude-opus-4-8",
) -> FileModifiedEvent:
    return FileModifiedEvent(
        ts=ts,
        session_id=session_id,
        is_subagent=is_subagent,
        project_tag=project_tag,
        source_path="/x/session.jsonl",
        file_path=file_path,
        op=op,
        old_string="old",
        new_string="new",
        replace_all=False,
        full_content=None,
        model=model,
        used_thinking=used_thinking,
        thinking_chars=thinking_chars,
    )


def _cmd_event(
    command: str = "ls -la",
    session_id: str = "sess1234abcd",
    project_tag: str = "myproj",
    is_subagent: bool = False,
    ts: datetime | None = TS,
) -> CommandEvent:
    return CommandEvent(
        ts=ts,
        session_id=session_id,
        is_subagent=is_subagent,
        project_tag=project_tag,
        source_path="/x/session.jsonl",
        command=command,
    )


# ---------------------------------------------------------------------------
# Test 1: file event round-trips correctly
# ---------------------------------------------------------------------------


class TestRecordAndReloadFileEvent:
    def test_record_and_reload_file_event(self, tmp_path: Path):
        db = CacheDB(tmp_path / "cache.db")
        evt = _file_event(
            file_path="/repo/bar.py",
            op=FileOp.EDIT,
            session_id="sessABCD1234",
            project_tag="coolproj",
            is_subagent=True,
            used_thinking=True,
            thinking_chars=512,
            ts=TS,
            model="claude-sonnet-4-6",
        )
        db.record_file_event(evt, max_rows=10)
        loaded = db.load_file_events()
        db.close()

        assert len(loaded) == 1
        r = loaded[0]
        assert r.file_path == "/repo/bar.py"
        assert r.op == FileOp.EDIT
        assert r.session_id == "sessABCD1234"
        assert r.project_tag == "coolproj"
        assert r.is_subagent is True
        assert r.used_thinking is True
        assert r.thinking_chars == 512
        assert r.ts == TS
        assert r.model == "claude-sonnet-4-6"
        assert r.old_string == "old"
        assert r.new_string == "new"
        assert r.replace_all is False

    def test_write_event_full_content_roundtrips(self, tmp_path: Path):
        db = CacheDB(tmp_path / "cache.db")
        evt = FileModifiedEvent(
            ts=TS,
            session_id="sessWRITE111",
            is_subagent=False,
            project_tag="writeproj",
            source_path="/x/s.jsonl",
            file_path="/repo/newfile.py",
            op=FileOp.WRITE,
            full_content="import os\nprint('hello')",
            model="claude-opus-4-8",
        )
        db.record_file_event(evt, max_rows=10)
        loaded = db.load_file_events()
        db.close()

        assert len(loaded) == 1
        r = loaded[0]
        assert r.op == FileOp.WRITE
        assert r.full_content == "import os\nprint('hello')"
        assert r.old_string is None
        assert r.new_string is None


# ---------------------------------------------------------------------------
# Test 2: command event round-trips correctly
# ---------------------------------------------------------------------------


class TestRecordAndReloadCommandEvent:
    def test_record_and_reload_command_event(self, tmp_path: Path):
        db = CacheDB(tmp_path / "cache.db")
        evt = _cmd_event(
            command="pytest -q --tb=short",
            session_id="sessXYZ9999",
            project_tag="testproj",
            is_subagent=True,
            ts=TS,
        )
        db.record_command_event(evt, max_rows=10)
        loaded = db.load_command_events()
        db.close()

        assert len(loaded) == 1
        r = loaded[0]
        assert r.command == "pytest -q --tb=short"
        assert r.session_id == "sessXYZ9999"
        assert r.project_tag == "testproj"
        assert r.is_subagent is True
        assert r.ts == TS


# ---------------------------------------------------------------------------
# Test 3: file events trimmed to max_rows
# ---------------------------------------------------------------------------


class TestFileEventsTrimmedToMax:
    def test_file_events_trimmed_to_max(self, tmp_path: Path):
        db = CacheDB(tmp_path / "cache.db")
        for i in range(10):
            evt = _file_event(file_path=f"/repo/file_{i}.py")
            db.record_file_event(evt, max_rows=3)
        loaded = db.load_file_events()
        db.close()

        assert len(loaded) == 3
        # The 3 most-recent (highest indices) must survive.
        paths = [e.file_path for e in loaded]
        assert "/repo/file_7.py" in paths
        assert "/repo/file_8.py" in paths
        assert "/repo/file_9.py" in paths
        # The oldest must be gone.
        assert "/repo/file_0.py" not in paths


# ---------------------------------------------------------------------------
# Test 4: command events trimmed to max_rows
# ---------------------------------------------------------------------------


class TestCommandEventsTrimmedToMax:
    def test_command_events_trimmed_to_max(self, tmp_path: Path):
        db = CacheDB(tmp_path / "cache.db")
        for i in range(10):
            evt = _cmd_event(command=f"step-{i}")
            db.record_command_event(evt, max_rows=5)
        loaded = db.load_command_events()
        db.close()

        assert len(loaded) == 5
        cmds = [e.command for e in loaded]
        assert "step-9" in cmds
        assert "step-0" not in cmds


# ---------------------------------------------------------------------------
# Test 5: load order is oldest-first (for correct replay)
# ---------------------------------------------------------------------------


class TestLoadOrderIsOldestFirst:
    def test_load_order_is_oldest_first(self, tmp_path: Path):
        db = CacheDB(tmp_path / "cache.db")
        paths = ["/repo/alpha.py", "/repo/beta.py", "/repo/gamma.py"]
        for p in paths:
            db.record_file_event(_file_event(file_path=p), max_rows=10)
        loaded = db.load_file_events()
        db.close()

        assert [e.file_path for e in loaded] == paths

    def test_command_load_order_is_oldest_first(self, tmp_path: Path):
        db = CacheDB(tmp_path / "cache.db")
        cmds = ["first-cmd", "second-cmd", "third-cmd"]
        for c in cmds:
            db.record_command_event(_cmd_event(command=c), max_rows=10)
        loaded = db.load_command_events()
        db.close()

        assert [e.command for e in loaded] == cmds


# ---------------------------------------------------------------------------
# Test 6: corrupt row is skipped silently
# ---------------------------------------------------------------------------


class TestCorruptRowSkipped:
    def test_corrupt_file_event_row_skipped(self, tmp_path: Path):
        db = CacheDB(tmp_path / "cache.db")
        # Insert a valid event first.
        db.record_file_event(_file_event(file_path="/repo/valid.py"), max_rows=10)
        # Inject a malformed JSON row directly.
        db._conn.execute(
            "INSERT INTO file_events (event_json) VALUES (?)",
            ("{not valid json!!!",),
        )
        db._conn.commit()
        # Should not raise; corrupt row is silently skipped.
        loaded = db.load_file_events()
        db.close()

        assert len(loaded) == 1
        assert loaded[0].file_path == "/repo/valid.py"

    def test_corrupt_command_event_row_skipped(self, tmp_path: Path):
        db = CacheDB(tmp_path / "cache.db")
        db.record_command_event(_cmd_event(command="good-cmd"), max_rows=10)
        db._conn.execute(
            "INSERT INTO command_events (event_json) VALUES (?)",
            ("{bad json here",),
        )
        db._conn.commit()
        loaded = db.load_command_events()
        db.close()

        assert len(loaded) == 1
        assert loaded[0].command == "good-cmd"


# ---------------------------------------------------------------------------
# Test 7: cache disabled when cache_path=None (app integration)
# ---------------------------------------------------------------------------


class TestCacheDisabledWhenPathNone:
    async def test_cache_disabled_when_path_none(self, tmp_path: Path):
        monitors_empty = tmp_path / "monitors_empty"
        monitors_empty.mkdir(parents=True, exist_ok=True)
        cfg = AppConfig(
            projects_root=tmp_path / "projects",
            active_window_seconds=3600,
            discovery_interval_seconds=0.05,
            poll_interval_seconds=0.05,
            cache_path=None,
            monitors_dir=monitors_empty,  # tests must not touch ~/.claude-visualizer/monitors/
        )
        app = VisualizerApp(cfg)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app._cache is None


# ---------------------------------------------------------------------------
# Test 8: cache persists across restarts
# ---------------------------------------------------------------------------


class TestCachePersistsAcrossRestarts:
    def test_cache_persists_across_restarts(self, tmp_path: Path):
        db_path = tmp_path / "persist.db"
        # Write via first instance.
        db1 = CacheDB(db_path)
        db1.record_file_event(_file_event(file_path="/repo/persisted.py"), max_rows=10)
        db1.record_command_event(_cmd_event(command="persisted-cmd"), max_rows=10)
        db1.close()

        # Open fresh instance on same path.
        db2 = CacheDB(db_path)
        file_events = db2.load_file_events()
        cmd_events = db2.load_command_events()
        db2.close()

        assert len(file_events) == 1
        assert file_events[0].file_path == "/repo/persisted.py"
        assert len(cmd_events) == 1
        assert cmd_events[0].command == "persisted-cmd"


# ---------------------------------------------------------------------------
# Test 9: ts=None round-trips to None (not the string "None")
# ---------------------------------------------------------------------------


class TestTsNoneRoundtrip:
    def test_file_event_ts_none_roundtrips(self, tmp_path: Path):
        db = CacheDB(tmp_path / "cache.db")
        evt = _file_event(ts=None)
        db.record_file_event(evt, max_rows=10)
        loaded = db.load_file_events()
        db.close()

        assert len(loaded) == 1
        assert loaded[0].ts is None

    def test_command_event_ts_none_roundtrips(self, tmp_path: Path):
        db = CacheDB(tmp_path / "cache.db")
        evt = _cmd_event(ts=None)
        db.record_command_event(evt, max_rows=10)
        loaded = db.load_command_events()
        db.close()

        assert len(loaded) == 1
        assert loaded[0].ts is None


# ---------------------------------------------------------------------------
# Test 10: tool_name round-trips correctly
# ---------------------------------------------------------------------------


class TestCommandEventToolNameRoundtrip:
    def test_command_event_tool_name_roundtrips(self, tmp_path: Path):
        db = CacheDB(tmp_path / "cache.db")
        evt = CommandEvent(
            ts=TS,
            session_id="sessABC12345",
            is_subagent=False,
            project_tag="myproj",
            source_path="/x/s.jsonl",
            command="MyServer::search query=auth",
            tool_name="mcp__X__y",
        )
        db.record_command_event(evt, max_rows=10)
        loaded = db.load_command_events()
        db.close()

        assert len(loaded) == 1
        assert loaded[0].tool_name == "mcp__X__y"

    def test_command_event_bash_default_tool_name(self, tmp_path: Path):
        db = CacheDB(tmp_path / "cache.db")
        evt = CommandEvent(
            ts=TS,
            session_id="sessDEF67890",
            is_subagent=False,
            project_tag="myproj",
            source_path="/x/s.jsonl",
            command="ls -la",
        )
        db.record_command_event(evt, max_rows=10)
        loaded = db.load_command_events()
        db.close()

        assert len(loaded) == 1
        assert loaded[0].tool_name == "Bash"
