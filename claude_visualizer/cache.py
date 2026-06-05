"""SQLite-backed persistence for the last N file events and command events.

Uses stdlib ``sqlite3`` only — no new package dependencies.  The cache is
opened once on app mount and closed on unmount.  A ``cache_path=None`` in
``AppConfig`` disables the cache entirely (the app runs without it).

On restart the app replays persisted events into all three panel models so
the UI resumes its last-seen state rather than starting blank.

Growth is bounded:
- Each ``record_*`` call trims the table to at most ``max_rows`` newest rows.
- ``DiffQueueModel._event_by_path`` is capped at ``cache_max_file_events``
  via the same ``OrderedDict`` LRU pattern used for ``_queue``.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import List

from claude_visualizer.events import CommandEvent, FileModifiedEvent, FileOp


class CacheDB:
    """SQLite-backed persistence for the last N file events and command events."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._create_tables()

    def _create_tables(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS file_events (
                id    INTEGER PRIMARY KEY AUTOINCREMENT,
                event_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS command_events (
                id    INTEGER PRIMARY KEY AUTOINCREMENT,
                event_json TEXT NOT NULL
            );
        """)
        self._conn.commit()

    # -- persistence ----------------------------------------------------------

    def record_file_event(self, event: FileModifiedEvent, max_rows: int) -> None:
        """Persist a FileModifiedEvent and trim the table to max_rows oldest-first."""
        self._conn.execute(
            "INSERT INTO file_events (event_json) VALUES (?)",
            (json.dumps(_serialize_file_event(event)),),
        )
        self._conn.execute(
            "DELETE FROM file_events WHERE id NOT IN "
            "(SELECT id FROM file_events ORDER BY id DESC LIMIT ?)",
            (max_rows,),
        )
        self._conn.commit()

    def record_command_event(self, event: CommandEvent, max_rows: int) -> None:
        """Persist a CommandEvent and trim the table to max_rows oldest-first."""
        self._conn.execute(
            "INSERT INTO command_events (event_json) VALUES (?)",
            (json.dumps(_serialize_command_event(event)),),
        )
        self._conn.execute(
            "DELETE FROM command_events WHERE id NOT IN "
            "(SELECT id FROM command_events ORDER BY id DESC LIMIT ?)",
            (max_rows,),
        )
        self._conn.commit()

    # -- loading --------------------------------------------------------------

    def load_file_events(self) -> List[FileModifiedEvent]:
        """Load persisted file events oldest-first (replay order)."""
        rows = self._conn.execute(
            "SELECT event_json FROM file_events ORDER BY id ASC"
        ).fetchall()
        events = []
        for (blob,) in rows:
            try:
                events.append(_deserialize_file_event(json.loads(blob)))
            except Exception:
                pass  # corrupt row — skip silently
        return events

    def load_command_events(self) -> List[CommandEvent]:
        """Load persisted command events oldest-first (replay order)."""
        rows = self._conn.execute(
            "SELECT event_json FROM command_events ORDER BY id ASC"
        ).fetchall()
        events = []
        for (blob,) in rows:
            try:
                events.append(_deserialize_command_event(json.loads(blob)))
            except Exception:
                pass  # corrupt row — skip silently
        return events

    def close(self) -> None:
        self._conn.close()


# -- serialization helpers ----------------------------------------------------


def _serialize_file_event(e: FileModifiedEvent) -> dict:
    return {
        "file_path": e.file_path,
        "op": e.op.value,
        "old_string": e.old_string,
        "new_string": e.new_string,
        "replace_all": e.replace_all,
        "full_content": e.full_content,
        "model": e.model,
        "session_id": e.session_id,
        "project_tag": e.project_tag,
        "is_subagent": e.is_subagent,
        "used_thinking": e.used_thinking,
        "thinking_chars": e.thinking_chars,
        "ts": e.ts.isoformat() if e.ts is not None else None,
    }


def _deserialize_file_event(d: dict) -> FileModifiedEvent:
    ts_raw = d.get("ts")
    ts = datetime.fromisoformat(ts_raw) if ts_raw else None
    return FileModifiedEvent(
        ts=ts,  # type: ignore[arg-type]  # ts may be None; display layer handles it
        session_id=d["session_id"],
        is_subagent=d.get("is_subagent", False),
        project_tag=d.get("project_tag", ""),
        source_path="",  # transcript path not meaningful on replay
        file_path=d["file_path"],
        op=FileOp(d["op"]),
        old_string=d.get("old_string"),
        new_string=d.get("new_string"),
        replace_all=d.get("replace_all", False),
        full_content=d.get("full_content"),
        model=d.get("model"),
        used_thinking=d.get("used_thinking", False),
        thinking_chars=d.get("thinking_chars", 0),
    )


def _serialize_command_event(e: CommandEvent) -> dict:
    return {
        "command": e.command,
        "session_id": e.session_id,
        "project_tag": e.project_tag,
        "is_subagent": e.is_subagent,
        "ts": e.ts.isoformat() if e.ts is not None else None,
        "tool_name": e.tool_name,
    }


def _deserialize_command_event(d: dict) -> CommandEvent:
    ts_raw = d.get("ts")
    ts = datetime.fromisoformat(ts_raw) if ts_raw else None
    return CommandEvent(
        ts=ts,  # type: ignore[arg-type]  # ts may be None; display layer handles it
        session_id=d["session_id"],
        is_subagent=d.get("is_subagent", False),
        project_tag=d.get("project_tag", ""),
        source_path="",  # transcript path not meaningful on replay
        command=d["command"],
        tool_name=d.get("tool_name", "Bash"),
    )
