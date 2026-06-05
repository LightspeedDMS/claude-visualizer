"""Tests for models/command_feed.py — the pure rolling command-feed model.

The model is UI-free (no ``textual`` import). It consumes
:class:`~claude_visualizer.events.CommandEvent` instances and maintains a
newest-on-top, NON-deduplicated, capacity-bounded log of Bash commands together
with the origin metadata the bottom panel renders (project tag, short session
id, subagent flag) and a timestamp.

Backing store: a ``collections.deque(maxlen=config.command_feed_max)`` so the
oldest entry is evicted automatically at capacity (AC4) — it is a LOG, not a
deduplicated list (AC2): identical commands each retain their own row.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from claude_visualizer.config import AppConfig
from claude_visualizer.events import CommandEvent
from claude_visualizer.models.command_feed import CommandFeedEntry, CommandFeedModel

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cmd(
    command: str = "ls -la",
    session_id: str = "abc123def456",
    project_tag: str = "proj",
    is_subagent: bool = False,
    ts: datetime | None = None,
) -> CommandEvent:
    return CommandEvent(
        ts=ts if ts is not None else datetime.now(timezone.utc),
        session_id=session_id,
        is_subagent=is_subagent,
        project_tag=project_tag,
        source_path="/src/x.jsonl",
        command=command,
        description=None,
    )


def _model(command_feed_max: int = 100) -> CommandFeedModel:
    return CommandFeedModel(AppConfig(command_feed_max=command_feed_max))


# ---------------------------------------------------------------------------
# Purity guard
# ---------------------------------------------------------------------------


class TestPurity:
    def test_no_textual_import(self):
        import claude_visualizer.models.command_feed as cf_mod

        source = Path(cf_mod.__file__).read_text(encoding="utf-8")
        assert "import textual" not in source
        assert "from textual" not in source


# ---------------------------------------------------------------------------
# CommandFeedModel construction
# ---------------------------------------------------------------------------


class TestModelConstruction:
    def test_empty_model_has_no_rows(self):
        assert _model().rows() == []


# ---------------------------------------------------------------------------
# record() — basic insertion + origin/timestamp capture (AC1/AC3)
# ---------------------------------------------------------------------------


class TestRecordInsertion:
    def test_single_record_appears(self):
        model = _model()
        model.record(_cmd(command="pytest -q"))
        rows = model.rows()
        assert len(rows) == 1
        assert rows[0].command == "pytest -q"

    def test_record_returns_entry(self):
        model = _model()
        entry = model.record(_cmd(command="echo hi"))
        assert isinstance(entry, CommandFeedEntry)
        assert entry.command == "echo hi"

    def test_origin_fields_captured(self):
        model = _model()
        model.record(
            _cmd(
                command="git status",
                session_id="sessionXYZ123456",
                project_tag="my-proj",
                is_subagent=True,
            )
        )
        entry = model.rows()[0]
        assert entry.command == "git status"
        assert entry.project_tag == "my-proj"
        assert entry.is_subagent is True

    def test_short_session_is_first_eight_chars(self):
        model = _model()
        model.record(_cmd(session_id="abcdefghijklmnop"))
        entry = model.rows()[0]
        assert entry.short_session == "abcdefgh"
        assert len(entry.short_session) == 8

    def test_short_session_shorter_than_eight(self):
        model = _model()
        model.record(_cmd(session_id="abc"))
        assert model.rows()[0].short_session == "abc"

    def test_timestamp_captured(self):
        ts = datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        model = _model()
        model.record(_cmd(ts=ts))
        assert model.rows()[0].ts == ts


# ---------------------------------------------------------------------------
# record() — ordering (newest-on-top, AC1)
# ---------------------------------------------------------------------------


class TestOrdering:
    def test_two_commands_newest_on_top(self):
        model = _model()
        model.record(_cmd(command="first"))
        model.record(_cmd(command="second"))
        assert [r.command for r in model.rows()] == ["second", "first"]

    def test_three_commands_ordering(self):
        model = _model()
        for c in ("a", "b", "c"):
            model.record(_cmd(command=c))
        assert [r.command for r in model.rows()] == ["c", "b", "a"]

    def test_commands_from_different_sessions_interleave_newest_on_top(self):
        # AC1: commands from ANY session appear, newest-on-top, regardless of
        # which session/subagent emitted them.
        model = _model()
        model.record(_cmd(command="sess1-cmd", session_id="session1xxx"))
        model.record(_cmd(command="sub-cmd", session_id="subxxxxx", is_subagent=True))
        model.record(_cmd(command="sess2-cmd", session_id="session2yyy"))
        rows = model.rows()
        assert [r.command for r in rows] == ["sess2-cmd", "sub-cmd", "sess1-cmd"]
        # The subagent row retains its subagent origin flag.
        assert rows[1].is_subagent is True


# ---------------------------------------------------------------------------
# record() — NO dedup (the feed is a log, AC2)
# ---------------------------------------------------------------------------


class TestNoDedup:
    def test_identical_command_produces_two_rows(self):
        model = _model()
        model.record(_cmd(command="npm test"))
        model.record(_cmd(command="npm test"))
        rows = model.rows()
        assert len(rows) == 2
        assert [r.command for r in rows] == ["npm test", "npm test"]

    def test_repeated_identical_commands_all_retained(self):
        model = _model()
        for _ in range(5):
            model.record(_cmd(command="git push"))
        rows = model.rows()
        assert len(rows) == 5
        assert all(r.command == "git push" for r in rows)

    def test_identical_commands_from_same_session_not_merged(self):
        # Same command AND same origin must still each get a row — there is no
        # coalescing of any kind in this log (contrast with the diff queue).
        model = _model()
        model.record(_cmd(command="ls", session_id="sameSESS01"))
        model.record(_cmd(command="ls", session_id="sameSESS01"))
        assert len(model.rows()) == 2


# ---------------------------------------------------------------------------
# record() — capacity fall-off (deque maxlen, AC4)
# ---------------------------------------------------------------------------


class TestCapacityFalloff:
    def test_capacity_enforced(self):
        model = _model(command_feed_max=3)
        for i in range(5):
            model.record(_cmd(command=f"cmd{i}"))
        assert len(model.rows()) == 3

    def test_oldest_falls_off_the_bottom(self):
        model = _model(command_feed_max=3)
        for c in ("a", "b", "c", "d"):
            model.record(_cmd(command=c))
        # "a" was oldest → evicted; newest-on-top order retained for the rest.
        commands = [r.command for r in model.rows()]
        assert "a" not in commands
        assert commands == ["d", "c", "b"]

    def test_capacity_one_keeps_only_newest(self):
        model = _model(command_feed_max=1)
        model.record(_cmd(command="old"))
        model.record(_cmd(command="new"))
        rows = model.rows()
        assert len(rows) == 1
        assert rows[0].command == "new"

    def test_overflow_with_duplicates_still_bounded(self):
        # No-dedup + bounded: 10 identical commands into a feed of 4 → exactly
        # 4 rows (oldest fall off) even though every command is identical.
        model = _model(command_feed_max=4)
        for _ in range(10):
            model.record(_cmd(command="same"))
        rows = model.rows()
        assert len(rows) == 4
        assert all(r.command == "same" for r in rows)


# ---------------------------------------------------------------------------
# rows() returns a snapshot (defensive copy)
# ---------------------------------------------------------------------------


class TestRowsSnapshot:
    def test_rows_is_a_list(self):
        model = _model()
        model.record(_cmd())
        assert isinstance(model.rows(), list)

    def test_mutating_returned_list_does_not_affect_model(self):
        model = _model()
        model.record(_cmd())
        rows = model.rows()
        rows.clear()
        assert len(model.rows()) == 1
