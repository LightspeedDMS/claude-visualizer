"""Tests for models/mru.py — the pure most-recently-used file model.

The model is UI-free (no textual import). It consumes FileModifiedEvent
instances and maintains a newest-first, deduplicated, capacity-bounded list
of files with their origin metadata for the MRU panel.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from claude_visualizer.config import AppConfig
from claude_visualizer.events import FileModifiedEvent, FileOp
from claude_visualizer.models.mru import MruEntry, MruModel

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_UNSET = object()  # sentinel so an explicit ``ts=None`` is preserved, not replaced


def _evt(
    file_path: str = "/proj/a.py",
    session_id: str = "abc123def456",
    project_tag: str = "proj",
    is_subagent: bool = False,
    op: FileOp = FileOp.WRITE,
    ts=_UNSET,
) -> FileModifiedEvent:
    return FileModifiedEvent(
        ts=datetime.now(timezone.utc) if ts is _UNSET else ts,
        session_id=session_id,
        is_subagent=is_subagent,
        project_tag=project_tag,
        source_path="/src/x.jsonl",
        file_path=file_path,
        op=op,
        full_content="x" if op is FileOp.WRITE else None,
    )


# ---------------------------------------------------------------------------
# Purity guard
# ---------------------------------------------------------------------------


class TestPurity:
    def test_no_textual_import(self):
        import claude_visualizer.models.mru as mru_mod

        source = Path(mru_mod.__file__).read_text(encoding="utf-8")
        assert "import textual" not in source
        assert "from textual" not in source


# ---------------------------------------------------------------------------
# MruModel construction
# ---------------------------------------------------------------------------


class TestModelConstruction:
    def test_empty_model_has_no_rows(self):
        model = MruModel(AppConfig())
        assert model.rows() == []

    def test_default_highlighted_path_is_none(self):
        model = MruModel(AppConfig())
        assert model.highlighted_path is None

    def test_highlighted_path_settable(self):
        model = MruModel(AppConfig())
        model.highlighted_path = "/proj/a.py"
        assert model.highlighted_path == "/proj/a.py"


# ---------------------------------------------------------------------------
# record() — basic insertion
# ---------------------------------------------------------------------------


class TestRecordInsertion:
    def test_single_record_appears(self):
        model = MruModel(AppConfig())
        model.record(_evt(file_path="/proj/a.py"))
        rows = model.rows()
        assert len(rows) == 1
        assert rows[0].file_path == "/proj/a.py"

    def test_record_returns_entry(self):
        model = MruModel(AppConfig())
        entry = model.record(_evt(file_path="/proj/a.py"))
        assert isinstance(entry, MruEntry)
        assert entry.file_path == "/proj/a.py"

    def test_origin_fields_captured(self):
        model = MruModel(AppConfig())
        model.record(
            _evt(
                file_path="/proj/a.py",
                session_id="sessionXYZ123456",
                project_tag="my-proj",
                is_subagent=True,
            )
        )
        entry = model.rows()[0]
        assert entry.project_tag == "my-proj"
        assert entry.is_subagent is True
        assert entry.file_path == "/proj/a.py"

    def test_short_session_is_first_eight_chars(self):
        model = MruModel(AppConfig())
        model.record(_evt(session_id="abcdefghijklmnop"))
        entry = model.rows()[0]
        assert entry.short_session == "abcdefgh"
        assert len(entry.short_session) == 8

    def test_short_session_shorter_than_eight(self):
        model = MruModel(AppConfig())
        model.record(_evt(session_id="abc"))
        entry = model.rows()[0]
        assert entry.short_session == "abc"

    def test_op_captured(self):
        model = MruModel(AppConfig())
        model.record(_evt(file_path="/x.py", op=FileOp.EDIT))
        assert model.rows()[0].op == FileOp.EDIT


# ---------------------------------------------------------------------------
# record() — per-row timestamp (post-epic UI enhancement)
# ---------------------------------------------------------------------------


class TestTimestamp:
    def test_ts_captured_from_event(self):
        ts = datetime(2024, 3, 4, 8, 9, 10, tzinfo=timezone.utc)
        model = MruModel(AppConfig())
        model.record(_evt(file_path="/x.py", ts=ts))
        assert model.rows()[0].ts == ts

    def test_ts_none_preserved(self):
        # An un-timestamped event must leave ``ts`` as None (the panel renders a
        # placeholder); record() must never fabricate a timestamp.
        model = MruModel(AppConfig())
        model.record(_evt(file_path="/x.py", ts=None))
        assert model.rows()[0].ts is None

    def test_repeat_refreshes_ts(self):
        # A repeat touch (move-to-front) refreshes ts like the other origin
        # fields, so the row shows the most recent modification time.
        old = datetime(2024, 3, 4, 8, 0, 0, tzinfo=timezone.utc)
        new = datetime(2024, 3, 4, 9, 30, 0, tzinfo=timezone.utc)
        model = MruModel(AppConfig())
        model.record(_evt(file_path="/a.py", ts=old))
        model.record(_evt(file_path="/a.py", ts=new))
        assert model.rows()[0].ts == new


# ---------------------------------------------------------------------------
# record() — ordering (newest first)
# ---------------------------------------------------------------------------


class TestOrdering:
    def test_two_distinct_files_newest_first(self):
        model = MruModel(AppConfig())
        model.record(_evt(file_path="/a.py"))
        model.record(_evt(file_path="/b.py"))
        rows = model.rows()
        assert [r.file_path for r in rows] == ["/b.py", "/a.py"]

    def test_three_files_ordering(self):
        model = MruModel(AppConfig())
        for path in ("/a.py", "/b.py", "/c.py"):
            model.record(_evt(file_path=path))
        assert [r.file_path for r in model.rows()] == ["/c.py", "/b.py", "/a.py"]


# ---------------------------------------------------------------------------
# record() — dedup / move-to-front
# ---------------------------------------------------------------------------


class TestDedupMoveToFront:
    def test_same_path_not_duplicated(self):
        model = MruModel(AppConfig())
        model.record(_evt(file_path="/a.py"))
        model.record(_evt(file_path="/a.py"))
        assert len(model.rows()) == 1

    def test_repeat_moves_to_front(self):
        model = MruModel(AppConfig())
        model.record(_evt(file_path="/a.py"))
        model.record(_evt(file_path="/b.py"))
        model.record(_evt(file_path="/c.py"))
        # Touch /a.py again → it should jump to the front.
        model.record(_evt(file_path="/a.py"))
        assert [r.file_path for r in model.rows()] == ["/a.py", "/c.py", "/b.py"]

    def test_repeat_updates_origin_fields(self):
        model = MruModel(AppConfig())
        model.record(_evt(file_path="/a.py", project_tag="old", op=FileOp.WRITE))
        model.record(_evt(file_path="/a.py", project_tag="new", op=FileOp.EDIT))
        entry = model.rows()[0]
        assert entry.project_tag == "new"
        assert entry.op == FileOp.EDIT
        assert len(model.rows()) == 1

    def test_repeat_updates_subagent_flag(self):
        model = MruModel(AppConfig())
        model.record(_evt(file_path="/a.py", is_subagent=False))
        model.record(_evt(file_path="/a.py", is_subagent=True))
        assert model.rows()[0].is_subagent is True


# ---------------------------------------------------------------------------
# record() — capacity fall-off (LRU)
# ---------------------------------------------------------------------------


class TestCapacityFalloff:
    def test_capacity_enforced(self):
        model = MruModel(AppConfig(mru_max=3))
        for i in range(5):
            model.record(_evt(file_path=f"/f{i}.py"))
        assert len(model.rows()) == 3

    def test_oldest_falls_off(self):
        model = MruModel(AppConfig(mru_max=3))
        for path in ("/a.py", "/b.py", "/c.py", "/d.py"):
            model.record(_evt(file_path=path))
        # /a.py is the least-recently-used → evicted.
        paths = [r.file_path for r in model.rows()]
        assert "/a.py" not in paths
        assert paths == ["/d.py", "/c.py", "/b.py"]

    def test_move_to_front_protects_from_falloff(self):
        model = MruModel(AppConfig(mru_max=3))
        model.record(_evt(file_path="/a.py"))
        model.record(_evt(file_path="/b.py"))
        model.record(_evt(file_path="/c.py"))
        # Re-touch /a.py so it is now most-recent, then push a new file.
        model.record(_evt(file_path="/a.py"))
        model.record(_evt(file_path="/d.py"))
        paths = [r.file_path for r in model.rows()]
        # /b.py was the least-recently-used and should be evicted, not /a.py.
        assert "/a.py" in paths
        assert "/b.py" not in paths
        assert len(paths) == 3

    def test_capacity_one(self):
        model = MruModel(AppConfig(mru_max=1))
        model.record(_evt(file_path="/a.py"))
        model.record(_evt(file_path="/b.py"))
        rows = model.rows()
        assert len(rows) == 1
        assert rows[0].file_path == "/b.py"


# ---------------------------------------------------------------------------
# rows() returns a snapshot (defensive copy)
# ---------------------------------------------------------------------------


class TestRowsSnapshot:
    def test_rows_is_a_list(self):
        model = MruModel(AppConfig())
        model.record(_evt(file_path="/a.py"))
        assert isinstance(model.rows(), list)

    def test_mutating_returned_list_does_not_affect_model(self):
        model = MruModel(AppConfig())
        model.record(_evt(file_path="/a.py"))
        rows = model.rows()
        rows.clear()
        # Internal state must be unaffected by mutating the returned list.
        assert len(model.rows()) == 1
