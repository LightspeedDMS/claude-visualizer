"""Tests for ``diffing`` — pure diff computation for the Live Diff panel.

The module is deliberately UI-free (no ``textual`` import): it turns a
:class:`FileModifiedEvent` into a structured list of ``DiffSegment(kind, text)``
so the UI can render colour from data while the logic stays unit-testable.

- AC1: an Edit (old_string → new_string) becomes a unified line diff with
  ADD / DEL / CONTEXT segment kinds (stdlib ``difflib``).
- AC2: a Write (full_content) becomes labelled WHOLE-FILE ADDITIONS — every
  line is ADD, prefixed by an explicit whole-file-write marker, with NO
  fabricated before/after (no DEL segments).
- AC10: a diff longer than ``config.diff_max_lines`` is truncated to the cap
  with a trailing ``…(truncated, N more lines)`` footer segment.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from claude_visualizer.config import AppConfig
from claude_visualizer.diffing import (
    COLOR_FOR_KIND,
    WHOLE_FILE_WRITE_LABEL,
    DiffKind,
    DiffSegment,
    compute_diff,
)
from claude_visualizer.events import FileModifiedEvent, FileOp

TS = datetime(2024, 1, 1, tzinfo=timezone.utc)


def _edit(old: str, new: str, **kw) -> FileModifiedEvent:
    base = dict(
        ts=TS,
        session_id="s",
        is_subagent=False,
        project_tag="p",
        source_path="/x.jsonl",
        file_path="/repo/f.py",
        op=FileOp.EDIT,
        old_string=old,
        new_string=new,
        replace_all=False,
    )
    base.update(kw)
    return FileModifiedEvent(**base)


def _write(content: str, **kw) -> FileModifiedEvent:
    base = dict(
        ts=TS,
        session_id="s",
        is_subagent=False,
        project_tag="p",
        source_path="/x.jsonl",
        file_path="/repo/f.py",
        op=FileOp.WRITE,
        full_content=content,
    )
    base.update(kw)
    return FileModifiedEvent(**base)


def _kinds(segments) -> list:
    return [s.kind for s in segments]


# ---------------------------------------------------------------------------
# DiffKind / DiffSegment / colour map
# ---------------------------------------------------------------------------


class TestDiffPrimitives:
    def test_kinds_exist(self):
        assert {DiffKind.ADD, DiffKind.DEL, DiffKind.CONTEXT, DiffKind.TRUNCATION}

    def test_segment_is_kind_plus_text(self):
        seg = DiffSegment(kind=DiffKind.ADD, text="hello")
        assert seg.kind is DiffKind.ADD
        assert seg.text == "hello"

    def test_segment_is_frozen(self):
        seg = DiffSegment(kind=DiffKind.ADD, text="x")
        with pytest.raises(Exception):
            seg.text = "y"  # type: ignore

    def test_color_map_add_green(self):
        assert COLOR_FOR_KIND[DiffKind.ADD] == "green"

    def test_color_map_del_red(self):
        assert COLOR_FOR_KIND[DiffKind.DEL] == "red"

    def test_color_map_context_dim(self):
        assert COLOR_FOR_KIND[DiffKind.CONTEXT] == "dim"

    def test_color_map_covers_every_kind(self):
        for kind in DiffKind:
            assert kind in COLOR_FOR_KIND


# ---------------------------------------------------------------------------
# AC1: Edit → unified diff with ADD / DEL / CONTEXT
# ---------------------------------------------------------------------------


class TestEditDiff:
    def test_single_line_change_has_del_and_add(self):
        segs = compute_diff(_edit("alpha", "beta"), AppConfig())
        kinds = _kinds(segs)
        assert DiffKind.DEL in kinds
        assert DiffKind.ADD in kinds

    def test_removed_line_text_preserved(self):
        segs = compute_diff(_edit("alpha", "beta"), AppConfig())
        dels = [s.text for s in segs if s.kind is DiffKind.DEL]
        assert any("alpha" in t for t in dels)

    def test_added_line_text_preserved(self):
        segs = compute_diff(_edit("alpha", "beta"), AppConfig())
        adds = [s.text for s in segs if s.kind is DiffKind.ADD]
        assert any("beta" in t for t in adds)

    def test_context_line_is_dim_kind(self):
        # Two lines; only the second changes → first is an unchanged CONTEXT.
        old = "keep me\nold line"
        new = "keep me\nnew line"
        segs = compute_diff(_edit(old, new), AppConfig())
        kinds = _kinds(segs)
        assert DiffKind.CONTEXT in kinds
        ctx = [s.text for s in segs if s.kind is DiffKind.CONTEXT]
        assert any("keep me" in t for t in ctx)

    def test_pure_addition_within_edit(self):
        # old has 1 line, new has 2 → the extra line is an ADD, no DEL needed.
        segs = compute_diff(_edit("line one", "line one\nline two"), AppConfig())
        kinds = _kinds(segs)
        assert DiffKind.ADD in kinds
        adds = [s.text for s in segs if s.kind is DiffKind.ADD]
        assert any("line two" in t for t in adds)

    def test_multiline_replacement_counts(self):
        old = "a\nb\nc"
        new = "a\nB\nc"
        segs = compute_diff(_edit(old, new), AppConfig())
        # Exactly one line (b→B) changed: one DEL and one ADD for that line.
        assert sum(1 for s in segs if s.kind is DiffKind.DEL) == 1
        assert sum(1 for s in segs if s.kind is DiffKind.ADD) == 1
        # The unchanged 'a' and 'c' are CONTEXT.
        assert sum(1 for s in segs if s.kind is DiffKind.CONTEXT) == 2

    def test_no_textual_addition_when_strings_equal(self):
        # Identical old/new → no ADD or DEL, only context (degenerate but valid).
        segs = compute_diff(_edit("same", "same"), AppConfig())
        kinds = _kinds(segs)
        assert DiffKind.ADD not in kinds
        assert DiffKind.DEL not in kinds

    def test_pure_deletion_emits_del_without_add(self):
        # Middle line removed with no replacement → a DEL for 'b', no ADD.
        segs = compute_diff(_edit("a\nb\nc", "a\nc"), AppConfig())
        dels = [s.text for s in segs if s.kind is DiffKind.DEL]
        assert any("b" in t for t in dels)
        assert not [s for s in segs if s.kind is DiffKind.ADD]
        # The surviving 'a' and 'c' are unchanged context.
        assert sum(1 for s in segs if s.kind is DiffKind.CONTEXT) == 2


# ---------------------------------------------------------------------------
# AC2: Write → labelled whole-file additions, no fabricated before-state
# ---------------------------------------------------------------------------


class TestWriteDiff:
    def test_all_body_lines_are_additions(self):
        segs = compute_diff(_write("one\ntwo\nthree"), AppConfig())
        body = [s for s in segs if s.kind in (DiffKind.ADD, DiffKind.DEL)]
        assert body, "expected body segments"
        assert all(s.kind is DiffKind.ADD for s in body)

    def test_no_deletions_no_fabricated_before_state(self):
        segs = compute_diff(_write("one\ntwo"), AppConfig())
        assert all(s.kind is not DiffKind.DEL for s in segs)

    def test_whole_file_write_header_present(self):
        segs = compute_diff(_write("x"), AppConfig())
        headers = [s for s in segs if s.kind is DiffKind.HEADER]
        assert headers, "expected a whole-file-write header segment"
        assert WHOLE_FILE_WRITE_LABEL in headers[0].text

    def test_every_content_line_rendered_as_add(self):
        content = "l1\nl2\nl3\nl4"
        segs = compute_diff(_write(content), AppConfig())
        adds = [s.text for s in segs if s.kind is DiffKind.ADD]
        for line in content.splitlines():
            assert any(line in a for a in adds)

    def test_empty_write_still_has_header(self):
        segs = compute_diff(_write(""), AppConfig())
        assert any(s.kind is DiffKind.HEADER for s in segs)

    def test_none_content_write_does_not_crash(self):
        # Defensive: a Write whose full_content is None yields just the header.
        segs = compute_diff(_write(content=None), AppConfig())
        assert any(s.kind is DiffKind.HEADER for s in segs)
        assert all(s.kind is not DiffKind.DEL for s in segs)


# ---------------------------------------------------------------------------
# AC10: truncation to diff_max_lines with a footer
# ---------------------------------------------------------------------------


class TestTruncation:
    def test_write_truncated_to_cap(self):
        cfg = AppConfig(diff_max_lines=10)
        content = "\n".join(f"line{i}" for i in range(100))
        segs = compute_diff(_write(content), cfg)
        # Body (ADD) segments must not exceed the cap.
        adds = [s for s in segs if s.kind is DiffKind.ADD]
        assert len(adds) <= cfg.diff_max_lines

    def test_truncation_footer_present_and_counts(self):
        cfg = AppConfig(diff_max_lines=10)
        content = "\n".join(f"line{i}" for i in range(100))
        segs = compute_diff(_write(content), cfg)
        footers = [s for s in segs if s.kind is DiffKind.TRUNCATION]
        assert len(footers) == 1
        assert "truncated" in footers[0].text.lower()
        # 100 body lines capped at 10 → 90 omitted.
        assert "90" in footers[0].text

    def test_no_footer_when_within_cap(self):
        cfg = AppConfig(diff_max_lines=500)
        segs = compute_diff(_write("a\nb\nc"), cfg)
        assert not [s for s in segs if s.kind is DiffKind.TRUNCATION]

    def test_edit_diff_truncated(self):
        cfg = AppConfig(diff_max_lines=5)
        old = "\n".join(f"o{i}" for i in range(50))
        new = "\n".join(f"n{i}" for i in range(50))
        segs = compute_diff(_edit(old, new), cfg)
        body = [
            s for s in segs if s.kind in (DiffKind.ADD, DiffKind.DEL, DiffKind.CONTEXT)
        ]
        assert len(body) <= cfg.diff_max_lines
        assert [s for s in segs if s.kind is DiffKind.TRUNCATION]

    def test_truncation_is_last_segment(self):
        cfg = AppConfig(diff_max_lines=3)
        segs = compute_diff(_write("\n".join(str(i) for i in range(20))), cfg)
        assert segs[-1].kind is DiffKind.TRUNCATION


class TestLineNumbers:
    """line_no population on DiffSegment — gutter numbers for the Diff panel."""

    # --- DiffSegment default ---

    def test_segment_default_line_no_is_none(self):
        """Existing call-sites without line_no still work (default=None)."""
        seg = DiffSegment(kind=DiffKind.ADD, text="+ x")
        assert seg.line_no is None

    def test_segment_accepts_explicit_line_no(self):
        seg = DiffSegment(kind=DiffKind.ADD, text="+ x", line_no=42)
        assert seg.line_no == 42

    # --- Edit: all body segments have line_no=None (relative offsets are misleading) ---

    def test_edit_context_line_no_is_none(self):
        # Edit CONTEXT segments must NOT carry line numbers (offsets are snippet-relative,
        # not true file positions, so they would be misleading).
        segs = compute_diff(_edit("keep\nold", "keep\nnew"), AppConfig())
        ctx = [s for s in segs if s.kind is DiffKind.CONTEXT]
        assert ctx, "expected a CONTEXT segment"
        assert all(s.line_no is None for s in ctx)

    def test_edit_context_multi_line_all_none(self):
        old = "a\nkeep1\nkeep2\nb"
        new = "a\nkeep1\nkeep2\nc"
        segs = compute_diff(_edit(old, new), AppConfig())
        ctx = [s for s in segs if s.kind is DiffKind.CONTEXT]
        assert all(s.line_no is None for s in ctx)

    # --- Edit: DEL has line_no=None ---

    def test_edit_del_line_no_is_none(self):
        segs = compute_diff(_edit("alpha\nbeta", "alpha\ngamma"), AppConfig())
        dels = [s for s in segs if s.kind is DiffKind.DEL]
        assert dels, "expected a DEL segment"
        assert all(s.line_no is None for s in dels)

    def test_edit_del_first_line_no_is_none(self):
        segs = compute_diff(_edit("removed\nkept", "kept"), AppConfig())
        dels = [s for s in segs if s.kind is DiffKind.DEL]
        assert all(s.line_no is None for s in dels)

    # --- Edit: ADD has line_no=None ---

    def test_edit_add_line_no_is_none(self):
        segs = compute_diff(_edit("alpha", "alpha\nextra"), AppConfig())
        adds = [s for s in segs if s.kind is DiffKind.ADD]
        assert adds, "expected an ADD segment"
        assert all(s.line_no is None for s in adds)

    def test_edit_add_first_line_no_is_none(self):
        segs = compute_diff(_edit("old line", "new line"), AppConfig())
        adds = [s for s in segs if s.kind is DiffKind.ADD]
        assert all(s.line_no is None for s in adds)

    def test_edit_replace_all_segments_line_no_none(self):
        # All DEL and ADD segments from a replace opcode must have line_no=None.
        old = "a\nb\nc"
        new = "a\nB\nc"
        segs = compute_diff(_edit(old, new), AppConfig())
        dels = [s for s in segs if s.kind is DiffKind.DEL]
        adds = [s for s in segs if s.kind is DiffKind.ADD]
        assert all(s.line_no is None for s in dels)
        assert all(s.line_no is None for s in adds)

    # --- Write: body lines get sequential 1-based numbers ---

    def test_write_body_lines_have_sequential_line_numbers(self):
        segs = compute_diff(_write("one\ntwo\nthree"), AppConfig())
        adds = [s for s in segs if s.kind is DiffKind.ADD]
        assert len(adds) == 3
        assert adds[0].line_no == 1
        assert adds[1].line_no == 2
        assert adds[2].line_no == 3

    def test_write_single_line_has_line_no_1(self):
        segs = compute_diff(_write("only"), AppConfig())
        adds = [s for s in segs if s.kind is DiffKind.ADD]
        assert adds[0].line_no == 1

    # --- Write: HEADER has None ---

    def test_write_header_has_no_line_number(self):
        segs = compute_diff(_write("x"), AppConfig())
        headers = [s for s in segs if s.kind is DiffKind.HEADER]
        assert headers[0].line_no is None

    # --- TRUNCATION always None ---

    def test_truncation_segment_has_no_line_number(self):
        cfg = AppConfig(diff_max_lines=3)
        segs = compute_diff(_write("\n".join(str(i) for i in range(20))), cfg)
        truncations = [s for s in segs if s.kind is DiffKind.TRUNCATION]
        assert truncations, "expected a TRUNCATION segment"
        assert truncations[0].line_no is None


class TestPurity:
    """The diffing module must stay UI-free (logic/colour-as-data only)."""

    def test_no_textual_or_rich_import(self):
        import inspect

        import claude_visualizer.diffing as diffing

        src = inspect.getsource(diffing)
        assert "import textual" not in src
        assert "from textual" not in src
        assert "import rich" not in src
        assert "from rich" not in src
