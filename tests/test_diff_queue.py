"""Tests for ``DiffQueueModel`` — the FIFO display-queue state machine.

Pure and UI-free.  All timing is deterministic: a :class:`FakeClock` is
injected as the ``now`` source and advanced explicitly — there are NO real
sleeps anywhere.  The model owns dwell, auto-scroll, FIFO advance, coalesce,
idle-rest and overflow; the UI just calls :meth:`tick` and renders the
returned :class:`DisplayState`.

ACs exercised:
- AC5  coalesce by file (re-record updates in place, keeps queue position)
- AC6  dwell bounds (min/max) + auto-scroll offset advancing over the dwell
- AC7  idle rests on the latest modified file (no blanking)
- AC8  overflow drops the stalest UNSEEN entry, exposes ``plus_n_more``
"""

from __future__ import annotations

from datetime import datetime, timezone

from claude_visualizer.config import AppConfig
from claude_visualizer.diffing import DiffKind
from claude_visualizer.events import FileModifiedEvent, FileOp
from claude_visualizer.models.diff_queue import DiffQueueModel, DisplayState

TS = datetime(2024, 1, 1, tzinfo=timezone.utc)


class FakeClock:
    """Deterministic monotonic clock; advance with :meth:`advance`."""

    def __init__(self, start: float = 0.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _write(path: str, content: str = "x", **kw) -> FileModifiedEvent:
    base = dict(
        ts=TS,
        session_id="sess1234abcd",
        is_subagent=False,
        project_tag="proj",
        source_path="/x.jsonl",
        file_path=path,
        op=FileOp.WRITE,
        full_content=content,
        model="claude-opus-4-8",
    )
    base.update(kw)
    return FileModifiedEvent(**base)


def _edit(path: str, old: str, new: str, **kw) -> FileModifiedEvent:
    base = dict(
        ts=TS,
        session_id="sess1234abcd",
        is_subagent=False,
        project_tag="proj",
        source_path="/x.jsonl",
        file_path=path,
        op=FileOp.EDIT,
        old_string=old,
        new_string=new,
        replace_all=False,
        model="claude-opus-4-8",
    )
    base.update(kw)
    return FileModifiedEvent(**base)


def _tall_content(n: int) -> str:
    return "\n".join(f"line{i}" for i in range(n))


# ---------------------------------------------------------------------------
# Basic display
# ---------------------------------------------------------------------------


class TestBasicDisplay:
    def test_empty_queue_tick_returns_none_or_idle(self):
        clock = FakeClock()
        model = DiffQueueModel(AppConfig(), now=clock)
        state = model.tick(clock(), viewport_height=10)
        assert state is None or state.file_path is None

    def test_first_recorded_file_is_displayed(self):
        clock = FakeClock()
        model = DiffQueueModel(AppConfig(), now=clock)
        model.record(_write("/repo/a.py", "hello"))
        state = model.tick(clock(), viewport_height=10)
        assert isinstance(state, DisplayState)
        assert state.file_path == "/repo/a.py"

    def test_display_state_carries_header_fields(self):
        clock = FakeClock()
        model = DiffQueueModel(AppConfig(), now=clock)
        model.record(
            _write(
                "/repo/a.py",
                "hi",
                model="claude-opus-4-8",
                used_thinking=True,
                thinking_chars=42,
                is_subagent=True,
                project_tag="myproj",
                session_id="abcdefgh1234",
            )
        )
        s = model.tick(clock(), viewport_height=10)
        assert s.model == "claude-opus-4-8"
        assert s.used_thinking is True
        assert s.is_subagent is True
        assert s.project_tag == "myproj"
        assert s.short_session == "abcdefgh"  # first 8 chars
        assert s.op == FileOp.WRITE

    def test_display_state_includes_segments(self):
        clock = FakeClock()
        model = DiffQueueModel(AppConfig(), now=clock)
        model.record(_write("/repo/a.py", "one\ntwo"))
        s = model.tick(clock(), viewport_height=10)
        assert any(seg.kind is DiffKind.ADD for seg in s.segments)


# ---------------------------------------------------------------------------
# Per-displayed-file timestamp (post-epic UI enhancement)
# ---------------------------------------------------------------------------


class TestTimestamp:
    def test_display_state_carries_ts(self):
        clock = FakeClock()
        model = DiffQueueModel(AppConfig(), now=clock)
        model.record(_write("/repo/a.py", "hi", ts=TS))
        s = model.tick(clock(), viewport_height=10)
        assert s.ts == TS

    def test_display_state_ts_none_passes_through(self):
        # An un-timestamped event must surface as ts None on the state (the
        # header renders a placeholder); the model never fabricates a time.
        clock = FakeClock()
        model = DiffQueueModel(AppConfig(), now=clock)
        model.record(_write("/repo/a.py", "hi", ts=None))
        s = model.tick(clock(), viewport_height=10)
        assert s.ts is None

    def test_empty_state_ts_is_none(self):
        # Pre-activity: nothing queued → tick returns the populated empty state
        # (never None), whose ts is None.
        clock = FakeClock()
        model = DiffQueueModel(AppConfig(), now=clock)
        s = model.tick(clock(), viewport_height=10)
        assert s is not None
        assert s.ts is None

    def test_visible_segments_bounded_by_viewport(self):
        clock = FakeClock()
        model = DiffQueueModel(AppConfig(), now=clock)
        model.record(_write("/repo/a.py", _tall_content(100)))
        s = model.tick(clock(), viewport_height=8)
        assert len(s.visible_segments) <= 8


# ---------------------------------------------------------------------------
# AC5: coalesce by file
# ---------------------------------------------------------------------------


class TestCoalesceByFile:
    def test_reedit_updates_event_in_place(self):
        clock = FakeClock()
        model = DiffQueueModel(
            AppConfig(min_dwell_seconds=3, max_dwell_seconds=12), now=clock
        )
        # Two distinct files queued; A is shown first.
        model.record(_write("/repo/a.py", "a-v1"))
        model.record(_write("/repo/b.py", "b-v1"))
        model.tick(clock(), viewport_height=10)  # A displayed
        # Re-record B (still queued, not yet shown) with new content.
        model.record(_write("/repo/b.py", "b-v2-updated"))
        # B must still be the very next file (position preserved), with v2.
        clock.advance(13)  # force advance past max dwell
        s = model.tick(clock(), viewport_height=10)
        assert s.file_path == "/repo/b.py"
        adds = "\n".join(seg.text for seg in s.segments if seg.kind is DiffKind.ADD)
        assert "b-v2-updated" in adds
        assert "b-v1" not in adds

    def test_reedit_keeps_queue_position_not_reappended(self):
        clock = FakeClock()
        model = DiffQueueModel(
            AppConfig(min_dwell_seconds=0, max_dwell_seconds=1), now=clock
        )
        # Queue three behind the displayed one: order B, C, D.
        model.record(_write("/repo/A.py", "a"))
        model.record(_write("/repo/B.py", "b"))
        model.record(_write("/repo/C.py", "c"))
        model.record(_write("/repo/D.py", "d"))
        model.tick(clock(), viewport_height=10)  # A displayed
        # Re-edit B while still queued — must NOT jump to the back.
        model.record(_edit("/repo/B.py", "b", "b2"))
        order = []
        for _ in range(3):
            clock.advance(2)  # exceed max dwell → advance each tick
            s = model.tick(clock(), viewport_height=10)
            order.append(s.file_path)
        assert order == ["/repo/B.py", "/repo/C.py", "/repo/D.py"]

    def test_recording_currently_displayed_file_updates_it(self):
        clock = FakeClock()
        model = DiffQueueModel(AppConfig(), now=clock)
        model.record(_write("/repo/a.py", "v1"))
        model.tick(clock(), viewport_height=10)
        model.record(_write("/repo/a.py", "v2-new-content"))
        s = model.tick(clock(), viewport_height=10)
        assert s.file_path == "/repo/a.py"
        adds = "\n".join(seg.text for seg in s.segments if seg.kind is DiffKind.ADD)
        assert "v2-new-content" in adds


# ---------------------------------------------------------------------------
# AC6: dwell bounds + auto-scroll
# ---------------------------------------------------------------------------


class TestDwellBounds:
    def test_does_not_advance_before_min_dwell(self):
        clock = FakeClock()
        model = DiffQueueModel(
            AppConfig(min_dwell_seconds=3, max_dwell_seconds=12), now=clock
        )
        model.record(_write("/repo/a.py", "short"))
        model.record(_write("/repo/b.py", "short"))
        model.tick(clock(), viewport_height=10)  # show A
        clock.advance(1.0)  # < min dwell
        s = model.tick(clock(), viewport_height=10)
        assert s.file_path == "/repo/a.py"  # still A despite a queued B

    def test_advances_after_min_dwell_when_fully_shown(self):
        clock = FakeClock()
        model = DiffQueueModel(
            AppConfig(min_dwell_seconds=3, max_dwell_seconds=12), now=clock
        )
        # Short diff that fits the viewport → "fully shown" immediately.
        model.record(_write("/repo/a.py", "short"))
        model.record(_write("/repo/b.py", "short"))
        model.tick(clock(), viewport_height=50)  # show A, fully visible
        clock.advance(3.5)  # past min dwell
        s = model.tick(clock(), viewport_height=50)
        assert s.file_path == "/repo/b.py"

    def test_force_advances_at_max_dwell_even_if_not_fully_shown(self):
        clock = FakeClock()
        model = DiffQueueModel(
            AppConfig(min_dwell_seconds=3, max_dwell_seconds=12), now=clock
        )
        # A very tall diff in a tiny viewport: never "fully shown" quickly, but
        # max dwell must force the advance regardless.
        model.record(_write("/repo/a.py", _tall_content(500)))
        model.record(_write("/repo/b.py", "short"))
        model.tick(clock(), viewport_height=5)  # show A (cannot fully show)
        clock.advance(12.5)  # past max dwell
        s = model.tick(clock(), viewport_height=5)
        assert s.file_path == "/repo/b.py"

    def test_does_not_advance_with_empty_queue_even_past_max(self):
        clock = FakeClock()
        model = DiffQueueModel(
            AppConfig(min_dwell_seconds=3, max_dwell_seconds=12), now=clock
        )
        model.record(_write("/repo/only.py", "x"))
        model.tick(clock(), viewport_height=10)
        clock.advance(100)  # way past max dwell, but nothing else queued
        s = model.tick(clock(), viewport_height=10)
        assert s.file_path == "/repo/only.py"  # rests on it (AC7)


class TestAutoScroll:
    def test_scroll_offset_starts_at_zero(self):
        clock = FakeClock()
        model = DiffQueueModel(
            AppConfig(min_dwell_seconds=3, max_dwell_seconds=12), now=clock
        )
        model.record(_write("/repo/a.py", _tall_content(200)))
        s = model.tick(clock(), viewport_height=10)
        assert s.scroll_offset == 0

    def test_scroll_offset_advances_over_dwell(self):
        clock = FakeClock()
        model = DiffQueueModel(
            AppConfig(min_dwell_seconds=3, max_dwell_seconds=12), now=clock
        )
        model.record(_write("/repo/a.py", _tall_content(200)))
        model.tick(clock(), viewport_height=10)
        clock.advance(6.0)  # halfway through max dwell
        s = model.tick(clock(), viewport_height=10)
        assert s.scroll_offset > 0  # the window has scrolled down

    def test_scroll_window_follows_offset(self):
        clock = FakeClock()
        model = DiffQueueModel(
            AppConfig(min_dwell_seconds=3, max_dwell_seconds=12), now=clock
        )
        model.record(_write("/repo/a.py", _tall_content(200)))
        model.tick(clock(), viewport_height=10)
        clock.advance(11.0)  # near max dwell → near the bottom
        s = model.tick(clock(), viewport_height=10)
        # The visible window should now include later lines, not the first ones.
        joined = "\n".join(seg.text for seg in s.visible_segments)
        assert "line0" not in joined

    def test_short_diff_does_not_scroll(self):
        clock = FakeClock()
        model = DiffQueueModel(
            AppConfig(min_dwell_seconds=3, max_dwell_seconds=12), now=clock
        )
        model.record(_write("/repo/a.py", "one\ntwo"))
        model.tick(clock(), viewport_height=50)
        clock.advance(11.0)
        s = model.tick(clock(), viewport_height=50)
        assert s.scroll_offset == 0  # nothing to scroll; whole diff fits

    def test_zero_max_dwell_jumps_to_bottom(self):
        # Degenerate config: max_dwell == 0 → no time span to ramp over, so a
        # tall diff is shown fully scrolled (offset at the bottom) at once.
        clock = FakeClock()
        model = DiffQueueModel(
            AppConfig(min_dwell_seconds=0, max_dwell_seconds=0), now=clock
        )
        model.record(_write("/repo/a.py", _tall_content(100)))
        s = model.tick(clock(), viewport_height=10)
        assert s.scroll_offset == max(0, len(s.segments) - 10)
        assert s.scroll_offset > 0


# ---------------------------------------------------------------------------
# AC7: idle rests on latest
# ---------------------------------------------------------------------------


class TestIdleRestsOnLatest:
    def test_drained_queue_rests_on_last_shown(self):
        clock = FakeClock()
        model = DiffQueueModel(
            AppConfig(min_dwell_seconds=0, max_dwell_seconds=1), now=clock
        )
        model.record(_write("/repo/a.py", "a"))
        model.record(_write("/repo/b.py", "b"))
        model.tick(clock(), viewport_height=10)  # show A
        clock.advance(2)
        model.tick(clock(), viewport_height=10)  # advance to B
        clock.advance(2)
        s = model.tick(clock(), viewport_height=10)  # queue drained → rest on B
        assert s.file_path == "/repo/b.py"
        assert s.is_idle is True

    def test_idle_does_not_blank(self):
        clock = FakeClock()
        model = DiffQueueModel(
            AppConfig(min_dwell_seconds=0, max_dwell_seconds=1), now=clock
        )
        model.record(_write("/repo/a.py", "content-here"))
        model.tick(clock(), viewport_height=10)
        clock.advance(50)
        s = model.tick(clock(), viewport_height=10)
        assert s.file_path == "/repo/a.py"
        assert s.segments  # non-empty: not blanked

    def test_new_record_after_idle_becomes_next(self):
        clock = FakeClock()
        model = DiffQueueModel(
            AppConfig(min_dwell_seconds=0, max_dwell_seconds=1), now=clock
        )
        model.record(_write("/repo/a.py", "a"))
        model.tick(clock(), viewport_height=10)  # show A, queue now empty
        clock.advance(5)
        model.tick(clock(), viewport_height=10)  # idle on A
        model.record(_write("/repo/b.py", "b"))  # new activity arrives
        clock.advance(2)
        s = model.tick(clock(), viewport_height=10)
        assert s.file_path == "/repo/b.py"

    def test_latest_record_while_idle_updates_resting_diff(self):
        # Idle on A; a re-edit of A must refresh the resting diff in place.
        clock = FakeClock()
        model = DiffQueueModel(
            AppConfig(min_dwell_seconds=0, max_dwell_seconds=1), now=clock
        )
        model.record(_write("/repo/a.py", "a-old"))
        model.tick(clock(), viewport_height=10)
        clock.advance(5)
        model.tick(clock(), viewport_height=10)  # idle on A
        model.record(_write("/repo/a.py", "a-fresh-content"))
        s = model.tick(clock(), viewport_height=10)
        adds = "\n".join(seg.text for seg in s.segments if seg.kind is DiffKind.ADD)
        assert "a-fresh-content" in adds


# ---------------------------------------------------------------------------
# AC8: overflow drops stalest unseen + +N more
# ---------------------------------------------------------------------------


class TestOverflow:
    def test_queue_capped_at_diff_queue_max(self):
        clock = FakeClock()
        model = DiffQueueModel(
            AppConfig(diff_queue_max=3, min_dwell_seconds=3, max_dwell_seconds=12),
            now=clock,
        )
        # Show one, then flood the queue with far more than the cap.
        model.record(_write("/repo/shown.py", "x"))
        model.tick(clock(), viewport_height=10)  # /repo/shown.py is current
        for i in range(20):
            model.record(_write(f"/repo/f{i}.py", "x"))
        assert model.queued_count() <= 3

    def test_plus_n_more_reflects_overflow(self):
        clock = FakeClock()
        model = DiffQueueModel(
            AppConfig(diff_queue_max=3, min_dwell_seconds=3, max_dwell_seconds=12),
            now=clock,
        )
        model.record(_write("/repo/shown.py", "x"))
        model.tick(clock(), viewport_height=10)
        for i in range(10):
            model.record(_write(f"/repo/f{i}.py", "x"))
        s = model.tick(clock(), viewport_height=10)
        # 10 distinct files queued behind the shown one, cap=3 → 7 dropped/pending
        # surfaced via the indicator (never silent).
        assert s.plus_n_more >= 1

    def test_stalest_unseen_dropped_first(self):
        clock = FakeClock()
        model = DiffQueueModel(
            AppConfig(diff_queue_max=2, min_dwell_seconds=3, max_dwell_seconds=12),
            now=clock,
        )
        model.record(_write("/repo/shown.py", "x"))
        model.tick(clock(), viewport_height=10)
        # Queue: oldest then newer; overflow must drop the OLDEST unseen.
        model.record(_write("/repo/oldest.py", "x"))
        model.record(_write("/repo/middle.py", "x"))
        model.record(_write("/repo/newest.py", "x"))  # forces a drop
        # Advance through the survivors; the oldest must be gone.
        seen = []
        for _ in range(5):
            clock.advance(13)
            s = model.tick(clock(), viewport_height=10)
            seen.append(s.file_path)
        assert "/repo/oldest.py" not in seen
        assert "/repo/newest.py" in seen

    def test_plus_n_more_zero_when_within_cap(self):
        clock = FakeClock()
        model = DiffQueueModel(
            AppConfig(diff_queue_max=10, min_dwell_seconds=0, max_dwell_seconds=1),
            now=clock,
        )
        model.record(_write("/repo/a.py", "x"))
        model.tick(clock(), viewport_height=10)
        clock.advance(5)
        s = model.tick(clock(), viewport_height=10)  # drained, idle
        assert s.plus_n_more == 0


# ---------------------------------------------------------------------------
# Pin behaviour (click-to-pin feature)
# ---------------------------------------------------------------------------


class TestPinBehavior:
    def _edit_event(self, path="/repo/a.py", project="proj", session="sess1234"):
        return _edit(
            path,
            "x = 1\n",
            "x = 2\n",
            session_id=session,
            project_tag=project,
        )

    def test_pin_unknown_file_returns_false(self):
        config = AppConfig()
        q = DiffQueueModel(config, now=lambda: 0.0)
        assert q.pin("/nonexistent.py", 0.0) is False

    def test_pin_known_file_returns_true(self):
        config = AppConfig()
        q = DiffQueueModel(config, now=lambda: 0.0)
        q.record(self._edit_event())
        assert q.pin("/repo/a.py", 0.0) is True

    def test_pin_state_is_pinned_flag(self):
        config = AppConfig()
        t = [0.0]
        q = DiffQueueModel(config, now=lambda: t[0])
        q.record(self._edit_event())
        q.pin("/repo/a.py", t[0])
        state = q.tick(t[0], 20)
        assert state.is_pinned is True
        assert state.file_path == "/repo/a.py"

    def test_pin_holds_within_min_seconds_even_with_new_events(self):
        config = AppConfig(min_pin_seconds=10.0)
        t = [0.0]
        q = DiffQueueModel(config, now=lambda: t[0])
        q.record(self._edit_event("/repo/a.py"))
        q.pin("/repo/a.py", t[0])
        # New event arrives at t=5 (within pin window)
        t[0] = 5.0
        q.record(self._edit_event("/repo/b.py"))
        state = q.tick(t[0], 20)
        assert state.is_pinned is True
        assert state.file_path == "/repo/a.py"

    def test_pin_releases_after_min_seconds_when_new_event_arrives(self):
        config = AppConfig(min_pin_seconds=10.0)
        t = [0.0]
        q = DiffQueueModel(config, now=lambda: t[0])
        q.record(self._edit_event("/repo/a.py"))
        q.pin("/repo/a.py", t[0])
        # New event arrives at t=5, then we tick past 10 s
        t[0] = 5.0
        q.record(self._edit_event("/repo/b.py"))
        t[0] = 11.0
        state = q.tick(t[0], 20)
        # Pin released — should now show b.py (or whatever queue produces)
        assert state.is_pinned is False

    def test_pin_stays_without_new_event_even_after_min_seconds(self):
        config = AppConfig(min_pin_seconds=10.0)
        t = [0.0]
        q = DiffQueueModel(config, now=lambda: t[0])
        q.record(self._edit_event("/repo/a.py"))
        q.pin("/repo/a.py", t[0])
        # No new events; tick past 10 s
        t[0] = 15.0
        state = q.tick(t[0], 20)
        # Still pinned — no new event arrived
        assert state.is_pinned is True
        assert state.file_path == "/repo/a.py"

    def test_default_display_state_is_not_pinned(self):
        clock = FakeClock()
        model = DiffQueueModel(AppConfig(), now=clock)
        model.record(_write("/repo/a.py", "hello"))
        state = model.tick(clock(), viewport_height=10)
        assert state.is_pinned is False


# ---------------------------------------------------------------------------
# Pin scroll behaviour (mouse-wheel scroll on pinned diff)
# ---------------------------------------------------------------------------


class TestPinScroll:
    """scroll_pin_by() lets the user scroll a pinned diff with the mouse wheel."""

    def _tall_edit(
        self, path: str = "/repo/a.py", lines: int = 40
    ) -> FileModifiedEvent:
        """An Edit event whose diff produces enough segments to scroll."""
        old = _tall_content(lines)
        new = _tall_content(lines) + "\nextra"
        return _edit(path, old, new)

    def test_scroll_pin_by_moves_scroll_offset(self):
        """scroll_pin_by(2, 5) followed by tick returns scroll_offset == 2."""
        config = AppConfig(min_pin_seconds=60.0)
        t = [0.0]
        q = DiffQueueModel(config, now=lambda: t[0])
        q.record(self._tall_edit())
        q.pin("/repo/a.py", t[0])
        q.scroll_pin_by(2, 5)
        state = q.tick(t[0], 5)
        assert state is not None
        assert state.is_pinned is True
        assert state.scroll_offset == 2

    def test_scroll_pin_by_clamps_at_max(self):
        """scroll_pin_by(9999, 5) clamps to max_scroll, not beyond."""
        config = AppConfig(min_pin_seconds=60.0)
        t = [0.0]
        q = DiffQueueModel(config, now=lambda: t[0])
        q.record(self._tall_edit())
        q.pin("/repo/a.py", t[0])
        q.scroll_pin_by(9999, 5)
        state = q.tick(t[0], 5)
        assert state is not None
        max_scroll = max(0, len(state.segments) - max(1, 5))
        assert state.scroll_offset == max_scroll

    def test_scroll_pin_by_clamps_at_zero(self):
        """Scrolling up past 0 stays clamped at 0."""
        config = AppConfig(min_pin_seconds=60.0)
        t = [0.0]
        q = DiffQueueModel(config, now=lambda: t[0])
        q.record(self._tall_edit())
        q.pin("/repo/a.py", t[0])
        q.scroll_pin_by(5, 5)  # scroll down first
        q.scroll_pin_by(-9999, 5)  # scroll up past 0
        state = q.tick(t[0], 5)
        assert state is not None
        assert state.scroll_offset == 0

    def test_scroll_resets_on_new_pin(self):
        """Re-pinning the same file resets scroll_offset to 0."""
        config = AppConfig(min_pin_seconds=60.0)
        t = [0.0]
        q = DiffQueueModel(config, now=lambda: t[0])
        q.record(self._tall_edit())
        q.pin("/repo/a.py", t[0])
        q.scroll_pin_by(5, 5)
        # Re-pin — scroll must reset
        q.pin("/repo/a.py", t[0])
        state = q.tick(t[0], 5)
        assert state is not None
        assert state.scroll_offset == 0

    def test_scroll_ignored_when_not_pinned(self):
        """scroll_pin_by has no effect when no file is pinned."""
        config = AppConfig()
        t = [0.0]
        q = DiffQueueModel(config, now=lambda: t[0])
        q.record(self._tall_edit())
        # Do NOT call pin() — scroll should be a no-op
        q.scroll_pin_by(5, 10)
        # No exception; normal (non-pinned) state returned
        state = q.tick(t[0], 10)
        assert state is not None
        assert state.is_pinned is False


# ---------------------------------------------------------------------------
# fast_forward_to_latest — skip cache-replay animation on startup
# ---------------------------------------------------------------------------


class TestFastForwardToLatest:
    """fast_forward_to_latest() discards history and rests on the newest entry."""

    def test_fast_forward_promotes_last_queued_item(self):
        # Record 3 distinct files; fast_forward must keep only the last one as
        # _current and clear _queue entirely so no animation follows.
        clock = FakeClock()
        model = DiffQueueModel(AppConfig(), now=clock)
        model.record(_write("/repo/alpha.py", "a"))
        model.record(_write("/repo/beta.py", "b"))
        model.record(_write("/repo/gamma.py", "c"))
        model.fast_forward_to_latest(now=0.0)
        assert len(model._queue) == 0
        assert model._current is not None
        assert model._current.file_path == "/repo/gamma.py"

    def test_fast_forward_noop_when_queue_empty(self):
        # An empty queue must not raise and _current stays None.
        clock = FakeClock()
        model = DiffQueueModel(AppConfig(), now=clock)
        model.fast_forward_to_latest(now=0.0)  # must not raise
        assert model._current is None

    def test_fast_forward_tick_returns_latest_immediately(self):
        # After fast_forward the very first tick shows gamma and is idle
        # (queue is drained — nothing pending behind it).
        clock = FakeClock()
        model = DiffQueueModel(AppConfig(), now=clock)
        model.record(_write("/repo/alpha.py", "a"))
        model.record(_write("/repo/beta.py", "b"))
        model.record(_write("/repo/gamma.py", "c"))
        model.fast_forward_to_latest(now=0.0)
        state = model.tick(now=0.0, viewport_height=10)
        assert state is not None
        assert state.file_path == "/repo/gamma.py"
        assert state.is_idle is True

    def test_fast_forward_does_not_affect_event_by_path(self):
        # _event_by_path is the pin-cache; fast_forward must NOT clear it so
        # the user can still pin any of the replayed files via click/keyboard.
        clock = FakeClock()
        model = DiffQueueModel(AppConfig(), now=clock)
        model.record(_write("/repo/alpha.py", "a"))
        model.record(_write("/repo/beta.py", "b"))
        model.record(_write("/repo/gamma.py", "c"))
        model.fast_forward_to_latest(now=0.0)
        assert "/repo/alpha.py" in model._event_by_path
        assert "/repo/beta.py" in model._event_by_path
        assert "/repo/gamma.py" in model._event_by_path


# ---------------------------------------------------------------------------
# Purity guard
# ---------------------------------------------------------------------------


class TestPurity:
    def test_no_textual_import(self):
        import inspect

        import claude_visualizer.models.diff_queue as dq

        src = inspect.getsource(dq)
        assert "import textual" not in src
        assert "from textual" not in src
