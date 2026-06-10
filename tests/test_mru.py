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
# record() — event-key dedup (same path+ts = same key → move-to-front)
# ---------------------------------------------------------------------------
# NOTE: The old "dedup by file_path" behavior has been replaced by the
# event-key model: the key is (file_path, ts).  Same path with DIFFERENT
# timestamps produces separate entries.  Same path with the SAME timestamp
# (exact same key) still deduplicates (move-to-front).


class TestDedupMoveToFront:
    def test_same_key_deduplicated(self):
        """Same (file_path, ts) key is deduplicated — move-to-front."""
        ts = datetime(2025, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        model = MruModel(AppConfig())
        model.record(_evt(file_path="/a.py", ts=ts))
        model.record(_evt(file_path="/a.py", ts=ts))
        assert len(model.rows()) == 1

    def test_same_path_different_ts_is_two_entries(self):
        """Same path, different timestamp = two distinct event-key entries."""
        t1 = datetime(2025, 1, 1, 10, 0, 1, tzinfo=timezone.utc)
        t2 = datetime(2025, 1, 1, 10, 0, 2, tzinfo=timezone.utc)
        model = MruModel(AppConfig())
        model.record(_evt(file_path="/a.py", ts=t1))
        model.record(_evt(file_path="/a.py", ts=t2))
        assert len(model.rows()) == 2

    def test_four_distinct_events_all_appear(self):
        """Four events with distinct (path, ts) keys → four rows, newest arrival first."""
        t1 = datetime(2025, 1, 1, 10, 0, 1, tzinfo=timezone.utc)
        t2 = datetime(2025, 1, 1, 10, 0, 2, tzinfo=timezone.utc)
        t3 = datetime(2025, 1, 1, 10, 0, 3, tzinfo=timezone.utc)
        t4 = datetime(2025, 1, 1, 10, 0, 4, tzinfo=timezone.utc)
        model = MruModel(AppConfig())
        model.record(_evt(file_path="/a.py", ts=t1))
        model.record(_evt(file_path="/b.py", ts=t2))
        model.record(_evt(file_path="/c.py", ts=t3))
        model.record(_evt(file_path="/a.py", ts=t4))
        rows = model.rows()
        assert len(rows) == 4
        # Newest arrival (/a.py t4) is first
        assert rows[0].file_path == "/a.py"
        assert rows[0].ts == t4

    def test_same_key_move_to_front_updates_fields(self):
        """Re-recording the same (path, ts) key moves it to front and refreshes fields."""
        ts = datetime(2025, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        model = MruModel(AppConfig())
        model.record(_evt(file_path="/b.py", ts=ts))
        model.record(_evt(file_path="/a.py", ts=ts, project_tag="old", op=FileOp.WRITE))
        # Now re-record /a.py with same ts but updated fields
        model.record(_evt(file_path="/a.py", ts=ts, project_tag="new", op=FileOp.EDIT))
        # Still only 2 entries (same key deduped)
        assert len(model.rows()) == 2
        # /a.py (same key, re-recorded last) is at front
        entry = model.rows()[0]
        assert entry.file_path == "/a.py"
        assert entry.project_tag == "new"
        assert entry.op == FileOp.EDIT

    def test_same_key_updates_subagent_flag(self):
        """Re-recording same (path, ts) key refreshes is_subagent."""
        ts = datetime(2025, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        model = MruModel(AppConfig())
        model.record(_evt(file_path="/a.py", ts=ts, is_subagent=False))
        model.record(_evt(file_path="/a.py", ts=ts, is_subagent=True))
        assert len(model.rows()) == 1
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


class TestEventKeyBehavior:
    """Tests for the new (file_path, ts) event-key model (no dedup, arrival order)."""

    def test_event_key_property_exists(self):
        model = MruModel(AppConfig())
        entry = model.record(_evt(file_path="/a.py"))
        assert hasattr(entry, "event_key")

    def test_event_key_is_tuple_of_path_and_ts(self):
        ts = datetime(2025, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
        model = MruModel(AppConfig())
        entry = model.record(_evt(file_path="/a.py", ts=ts))
        assert entry.event_key == ("/a.py", ts)

    def test_same_path_different_ts_creates_two_entries(self):
        """Same file path with distinct timestamps is NOT deduplicated."""
        t1 = datetime(2025, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2025, 6, 1, 10, 0, 1, tzinfo=timezone.utc)
        model = MruModel(AppConfig())
        model.record(_evt(file_path="/a.py", ts=t1))
        model.record(_evt(file_path="/a.py", ts=t2))
        assert len(model.rows()) == 2

    def test_arrival_order_is_display_order_no_timestamp_sort(self):
        """rows() returns newest-arrival first (insertion order reversed), no ts sort."""
        t_old = datetime(2025, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
        t_new = datetime(2025, 6, 1, 10, 0, 1, tzinfo=timezone.utc)
        model = MruModel(AppConfig())
        # Record older timestamp SECOND — display should still put it first (latest arrival)
        model.record(_evt(file_path="/a.py", ts=t_new))
        model.record(_evt(file_path="/b.py", ts=t_old))
        rows = model.rows()
        # /b.py arrived last → appears first in output
        assert rows[0].file_path == "/b.py"
        assert rows[1].file_path == "/a.py"

    def test_highlighted_key_attribute_exists(self):
        model = MruModel(AppConfig())
        assert hasattr(model, "highlighted_key")

    def test_highlighted_key_default_is_none(self):
        model = MruModel(AppConfig())
        assert model.highlighted_key is None

    def test_highlighted_key_settable(self):
        ts = datetime(2025, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
        model = MruModel(AppConfig())
        model.highlighted_key = ("/a.py", ts)
        assert model.highlighted_key == ("/a.py", ts)

    def test_lru_eviction_by_arrival_order(self):
        """When at capacity, oldest-arrival entry is evicted, not oldest-timestamp."""
        t1 = datetime(2025, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2025, 6, 1, 10, 0, 1, tzinfo=timezone.utc)
        t3 = datetime(2025, 6, 1, 10, 0, 2, tzinfo=timezone.utc)
        # cap=2; record 3 events; first arrival (t1 with /a.py) gets evicted
        model = MruModel(AppConfig(mru_max=2))
        e1 = model.record(_evt(file_path="/a.py", ts=t1))
        model.record(_evt(file_path="/b.py", ts=t2))
        model.record(_evt(file_path="/c.py", ts=t3))
        keys = [r.event_key for r in model.rows()]
        assert e1.event_key not in keys  # oldest arrival evicted
        assert len(keys) == 2


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
# rows() — arrival-order display (event-key model, no timestamp sort)
# ---------------------------------------------------------------------------
# NOTE: The model now uses arrival order (insertion order reversed) — no
# timestamp sort.  The old timestamp-sort tests have been updated to reflect
# the new behavior.


class TestRowsTimestampOrdering:
    def test_rows_arrival_order_not_timestamp_order(self):
        """rows() returns newest-arrival first, regardless of event timestamps.

        Simulates: session A's tailer drains two events (18:09, 18:27),
        then session B's tailer drains one event (18:24).
        Arrival order: 18:09, 18:27, 18:24
        Expected display order (newest arrival first): 18:24, 18:27, 18:09
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
        # Display order is newest-arrival first: ev3 (18:24), ev2 (18:27), ev1 (18:09)
        assert rows[0].file_path == "/b/middle.py"  # arrived last → first
        assert rows[1].file_path == "/a/latest.py"  # arrived second → second
        assert rows[2].file_path == "/a/early.py"  # arrived first → last

    def test_rows_none_ts_arrival_order(self):
        """Entries with ts=None appear in arrival order with other entries."""
        model = MruModel(AppConfig())

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

        # ev_no_ts arrives first, ev_with_ts arrives second
        model.record(ev_no_ts)
        model.record(ev_with_ts)

        rows = model.rows()
        # Newest arrival (ev_with_ts) is first regardless of ts=None on the other
        assert rows[0].file_path == "/a/timestamped.py"  # arrived last → first
        assert rows[1].file_path == "/b/no_timestamp.py"  # arrived first → last
