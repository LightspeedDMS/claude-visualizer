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
        # Explicit timestamps so the sort is deterministic regardless of wall clock.
        t1 = datetime(2025, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2025, 1, 1, 10, 0, 1, tzinfo=timezone.utc)
        model = MruModel(AppConfig())
        model.record(_evt(file_path="/a.py", ts=t1))
        model.record(_evt(file_path="/b.py", ts=t2))
        rows = model.rows()
        assert [r.file_path for r in rows] == ["/b.py", "/a.py"]

    def test_three_files_ordering(self):
        model = MruModel(AppConfig())
        for i, path in enumerate(("/a.py", "/b.py", "/c.py")):
            ts = datetime(2025, 1, 1, 10, 0, i, tzinfo=timezone.utc)
            model.record(_evt(file_path=path, ts=ts))
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
        # Explicit timestamps: t1 < t2 < t3 < t4 so the second /a.py touch
        # (t4 = newest) sorts first, /c.py (t3) second, /b.py (t2) last.
        t1 = datetime(2025, 1, 1, 10, 0, 1, tzinfo=timezone.utc)
        t2 = datetime(2025, 1, 1, 10, 0, 2, tzinfo=timezone.utc)
        t3 = datetime(2025, 1, 1, 10, 0, 3, tzinfo=timezone.utc)
        t4 = datetime(2025, 1, 1, 10, 0, 4, tzinfo=timezone.utc)
        model = MruModel(AppConfig())
        model.record(_evt(file_path="/a.py", ts=t1))
        model.record(_evt(file_path="/b.py", ts=t2))
        model.record(_evt(file_path="/c.py", ts=t3))
        # Touch /a.py again with a newer timestamp → it should sort to the front.
        model.record(_evt(file_path="/a.py", ts=t4))
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
        # Explicit timestamps: t1 < t2 < t3 < t4 so /a.py (t1) is LRU → evicted,
        # and the remaining rows sort: /d.py (t4), /c.py (t3), /b.py (t2).
        timestamps = [
            datetime(2025, 1, 1, 10, 0, i, tzinfo=timezone.utc) for i in range(1, 5)
        ]
        model = MruModel(AppConfig(mru_max=3))
        for path, ts in zip(("/a.py", "/b.py", "/c.py", "/d.py"), timestamps):
            model.record(_evt(file_path=path, ts=ts))
        # /a.py is the least-recently-used → evicted.
        paths = [r.file_path for r in model.rows()]
        assert "/a.py" not in paths
        assert paths == ["/d.py", "/c.py", "/b.py"]

    def test_move_to_front_protects_from_falloff(self):
        # Explicit timestamps: t1..t5 monotonically increasing.
        # /a.py recorded at t1, /b.py at t2, /c.py at t3, /a.py again at t4,
        # /d.py at t5. LRU eviction is by arrival order (OrderedDict), so /b.py
        # (arrival-oldest after /a.py was moved) is evicted, not /a.py.
        t1 = datetime(2025, 1, 1, 10, 0, 1, tzinfo=timezone.utc)
        t2 = datetime(2025, 1, 1, 10, 0, 2, tzinfo=timezone.utc)
        t3 = datetime(2025, 1, 1, 10, 0, 3, tzinfo=timezone.utc)
        t4 = datetime(2025, 1, 1, 10, 0, 4, tzinfo=timezone.utc)
        t5 = datetime(2025, 1, 1, 10, 0, 5, tzinfo=timezone.utc)
        model = MruModel(AppConfig(mru_max=3))
        model.record(_evt(file_path="/a.py", ts=t1))
        model.record(_evt(file_path="/b.py", ts=t2))
        model.record(_evt(file_path="/c.py", ts=t3))
        # Re-touch /a.py so it is now most-recent, then push a new file.
        model.record(_evt(file_path="/a.py", ts=t4))
        model.record(_evt(file_path="/d.py", ts=t5))
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


# ---------------------------------------------------------------------------
# rows() — timestamp-based display ordering (cross-session interleave fix)
# ---------------------------------------------------------------------------


class TestRowsTimestampOrdering:
    def test_rows_sorted_by_timestamp_not_arrival(self):
        """Events arriving out-of-timestamp-order are displayed by timestamp.

        Simulates: session A's tailer drains two events (18:09, 18:27),
        then session B's tailer drains one event (18:24).
        Arrival order: 18:09, 18:27, 18:24
        Expected display order: 18:27, 18:24, 18:09
        """
        model = MruModel(AppConfig())

        ev1 = FileModifiedEvent(
            file_path="/a/early.py",
            op=FileOp.EDIT,
            session_id="sess-aaa",
            project_tag="projA",
            is_subagent=False,
            source_path="/src/a.jsonl",
            old_string="x",
            new_string="y",
            ts=datetime(2025, 1, 1, 18, 9, 56, tzinfo=timezone.utc),
        )
        ev2 = FileModifiedEvent(
            file_path="/a/latest.py",
            op=FileOp.EDIT,
            session_id="sess-aaa",
            project_tag="projA",
            is_subagent=False,
            source_path="/src/a.jsonl",
            old_string="x",
            new_string="y",
            ts=datetime(2025, 1, 1, 18, 27, 2, tzinfo=timezone.utc),
        )
        ev3 = FileModifiedEvent(
            file_path="/b/middle.py",
            op=FileOp.EDIT,
            session_id="sess-bbb",
            project_tag="projB",
            is_subagent=False,
            source_path="/src/b.jsonl",
            old_string="x",
            new_string="y",
            ts=datetime(2025, 1, 1, 18, 24, 11, tzinfo=timezone.utc),
        )

        model.record(ev1)  # arrives first (18:09)
        model.record(ev2)  # arrives second (18:27)
        model.record(ev3)  # arrives third (18:24)

        rows = model.rows()
        assert len(rows) == 3
        # Display order must be by timestamp descending: 18:27, 18:24, 18:09
        assert rows[0].file_path == "/a/latest.py"  # 18:27
        assert rows[1].file_path == "/b/middle.py"  # 18:24
        assert rows[2].file_path == "/a/early.py"  # 18:09

    def test_rows_none_ts_sorts_to_end(self):
        """Entries with ts=None appear after all timestamped entries."""
        model = MruModel(AppConfig())

        ev_with_ts = FileModifiedEvent(
            file_path="/a/timestamped.py",
            op=FileOp.EDIT,
            session_id="sess-aaa",
            project_tag="projA",
            is_subagent=False,
            source_path="/src/a.jsonl",
            old_string="x",
            new_string="y",
            ts=datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
        )
        ev_no_ts = FileModifiedEvent(
            file_path="/b/no_timestamp.py",
            op=FileOp.EDIT,
            session_id="sess-bbb",
            project_tag="projB",
            is_subagent=False,
            source_path="/src/b.jsonl",
            old_string="x",
            new_string="y",
            ts=None,
        )

        model.record(ev_no_ts)
        model.record(ev_with_ts)

        rows = model.rows()
        assert rows[0].file_path == "/a/timestamped.py"  # has ts → first
        assert rows[1].file_path == "/b/no_timestamp.py"  # None → last
