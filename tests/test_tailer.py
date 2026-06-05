"""Tests for tailer.py — incremental byte-offset file tailer.

All tests use REAL temporary files (anti-mock). The tailer must:
- emit only COMPLETE lines (split on \\n), buffering trailing fragments;
- advance size_seen to the last newline byte offset;
- reset on rotation (inode change) or truncation (current_size < size_seen);
- cold-start seed: first attach seeks max(0, size - seed_tail_bytes) and
  discards the first partial line (no full-history replay);
- drop lines exceeding config.max_line_bytes.
"""

from __future__ import annotations

import os
from pathlib import Path

from claude_visualizer.config import AppConfig
from claude_visualizer.tailer import TailState, read_new

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_bytes(path: Path, data: str, mode: str = "a") -> None:
    """Append (or write) raw text to a real file and flush to disk."""
    with open(path, mode, encoding="utf-8") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())


def _write_raw_bytes(path: Path, data: bytes, mode: str = "ab") -> None:
    """Append (or write) RAW bytes to a real file and flush to disk.

    Unlike :func:`_write_bytes`, this bypasses text encoding so tests can
    inject arbitrary byte sequences — including invalid UTF-8 — exactly as a
    Claude Code transcript can when it embeds raw file / tool-output content.
    """
    with open(path, mode) as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())


def _attach(path: Path) -> TailState:
    """Create a fresh TailState for a path (cold-start, nothing seen yet)."""
    return TailState(path=str(path))


def _small_seed_config() -> AppConfig:
    """Config with a tiny seed window so cold-start logic is exercisable."""
    return AppConfig(seed_tail_bytes=16, max_line_bytes=1_000_000)


# ---------------------------------------------------------------------------
# TailState construction
# ---------------------------------------------------------------------------


class TestTailStateConstruction:
    def test_defaults(self, tmp_path: Path):
        p = tmp_path / "s.jsonl"
        state = TailState(path=str(p))
        assert state.path == str(p)
        assert state.inode is None
        assert state.size_seen == 0
        assert state.partial_buffer == b""


# ---------------------------------------------------------------------------
# Cold-start seed
# ---------------------------------------------------------------------------


class TestColdStartSeed:
    """First attach seeks near the tail; no full-history replay."""

    def test_small_file_first_read_returns_all_complete_lines(self, tmp_path: Path):
        # File smaller than seed window → seek to 0 → all complete lines returned.
        p = tmp_path / "s.jsonl"
        _write_bytes(p, "a\nb\nc\n", mode="w")
        cfg = AppConfig(seed_tail_bytes=4096)
        state = _attach(p)
        lines = read_new(state, cfg)
        assert lines == ["a", "b", "c"]

    def test_large_file_first_read_seeds_near_tail_and_drops_partial(
        self, tmp_path: Path
    ):
        # seed_tail_bytes=16. File content is 30 bytes; seek to offset 14.
        # Offset 14 lands mid-line → first partial line discarded, only
        # subsequent complete lines surface.
        p = tmp_path / "s.jsonl"
        _write_bytes(p, "AAAAAAAAAA\nBBBB\nCCCC\n", mode="w")
        # bytes: "AAAAAAAAAA\n" = 11, "BBBB\n" = 5 (16), "CCCC\n" = 5 (21 total)
        # size=21, seed=16 → seek to 5 → lands at start of "BBBB\n"? offset 5
        # is mid first line ("AAAAAAAAAA"), so the leading partial is dropped.
        cfg = _small_seed_config()
        state = _attach(p)
        lines = read_new(state, cfg)
        # The first partial (cut "AAAAA...") is discarded; remaining complete
        # lines after the first newline are returned.
        assert "AAAAAAAAAA" not in lines
        assert "BBBB" in lines
        assert "CCCC" in lines

    def test_first_read_records_inode(self, tmp_path: Path):
        p = tmp_path / "s.jsonl"
        _write_bytes(p, "x\n", mode="w")
        cfg = AppConfig(seed_tail_bytes=4096)
        state = _attach(p)
        read_new(state, cfg)
        assert state.inode == os.stat(p).st_ino

    def test_seed_only_runs_once(self, tmp_path: Path):
        # After the first (seeding) read, subsequent reads do NOT re-seed;
        # they continue from size_seen.
        p = tmp_path / "s.jsonl"
        _write_bytes(p, "AAAAAAAAAA\nBBBB\n", mode="w")
        cfg = _small_seed_config()
        state = _attach(p)
        read_new(state, cfg)  # seeds
        _write_bytes(p, "DDDD\n")
        lines = read_new(state, cfg)
        assert lines == ["DDDD"]

    def test_seed_chunk_with_no_newline_drops_everything(self, tmp_path: Path):
        # A single very long line with NO newline, larger than the seed window.
        # Cold-start seeks mid-line; the seeded chunk contains no newline at
        # all → the entire partial head is discarded (not buffered, not
        # emitted). A later newline then completes a fresh line.
        p = tmp_path / "s.jsonl"
        _write_bytes(p, "Z" * 40, mode="w")  # 40 bytes, no newline
        cfg = _small_seed_config()  # seed=16 → seek to 24, mid-line
        state = _attach(p)
        assert read_new(state, cfg) == []
        assert state.partial_buffer == b""
        # Once a newline arrives, the NEXT complete line surfaces normally.
        _write_bytes(p, "\nnext\n")
        assert read_new(state, cfg) == ["next"]


# ---------------------------------------------------------------------------
# Append between reads
# ---------------------------------------------------------------------------


class TestAppendBetweenReads:
    def test_append_after_seed_returns_new_lines(self, tmp_path: Path):
        p = tmp_path / "s.jsonl"
        _write_bytes(p, "first\n", mode="w")
        cfg = AppConfig(seed_tail_bytes=4096)
        state = _attach(p)
        assert read_new(state, cfg) == ["first"]
        _write_bytes(p, "second\nthird\n")
        assert read_new(state, cfg) == ["second", "third"]

    def test_no_new_bytes_returns_empty(self, tmp_path: Path):
        p = tmp_path / "s.jsonl"
        _write_bytes(p, "only\n", mode="w")
        cfg = AppConfig(seed_tail_bytes=4096)
        state = _attach(p)
        read_new(state, cfg)
        assert read_new(state, cfg) == []

    def test_size_seen_advances_to_last_newline(self, tmp_path: Path):
        p = tmp_path / "s.jsonl"
        _write_bytes(p, "line1\n", mode="w")
        cfg = AppConfig(seed_tail_bytes=4096)
        state = _attach(p)
        read_new(state, cfg)
        assert state.size_seen == len("line1\n")


# ---------------------------------------------------------------------------
# Partial-line completion (NEVER parse a fragment)
# ---------------------------------------------------------------------------


class TestPartialLineCompletion:
    def test_trailing_fragment_buffered_not_returned(self, tmp_path: Path):
        p = tmp_path / "s.jsonl"
        _write_bytes(p, "done\n", mode="w")
        cfg = AppConfig(seed_tail_bytes=4096)
        state = _attach(p)
        read_new(state, cfg)
        # Write a line WITHOUT trailing newline → fragment must be buffered.
        _write_bytes(p, "partial-frag")
        assert read_new(state, cfg) == []
        assert state.partial_buffer == b"partial-frag"

    def test_fragment_completed_next_read(self, tmp_path: Path):
        p = tmp_path / "s.jsonl"
        _write_bytes(p, "done\n", mode="w")
        cfg = AppConfig(seed_tail_bytes=4096)
        state = _attach(p)
        read_new(state, cfg)
        _write_bytes(p, "half")
        assert read_new(state, cfg) == []
        _write_bytes(p, "-rest\n")
        assert read_new(state, cfg) == ["half-rest"]
        assert state.partial_buffer == b""

    def test_buffered_fragment_with_no_new_bytes_returns_empty(self, tmp_path: Path):
        # A fragment is buffered, then a read happens with NO new bytes on
        # disk (current_size == size_seen). The early no-op guard is skipped
        # because the buffer is non-empty, the file is re-opened, the read at
        # EOF yields "", and read_new returns [] while preserving the buffer.
        p = tmp_path / "s.jsonl"
        _write_bytes(p, "seed\n", mode="w")
        cfg = AppConfig(seed_tail_bytes=4096)
        state = _attach(p)
        read_new(state, cfg)
        _write_bytes(p, "frag-no-newline")
        assert read_new(state, cfg) == []
        assert state.partial_buffer == b"frag-no-newline"
        # Read again with nothing new appended → still [] and buffer intact.
        assert read_new(state, cfg) == []
        assert state.partial_buffer == b"frag-no-newline"

    def test_multiple_lines_with_trailing_fragment(self, tmp_path: Path):
        p = tmp_path / "s.jsonl"
        _write_bytes(p, "x\n", mode="w")
        cfg = AppConfig(seed_tail_bytes=4096)
        state = _attach(p)
        read_new(state, cfg)
        _write_bytes(p, "a\nb\nc-incomplete")
        assert read_new(state, cfg) == ["a", "b"]
        assert state.partial_buffer == b"c-incomplete"
        _write_bytes(p, "-now\n")
        assert read_new(state, cfg) == ["c-incomplete-now"]


# ---------------------------------------------------------------------------
# Rotation / truncation reset
# ---------------------------------------------------------------------------


class TestRotationReset:
    def test_truncation_resets_and_rereads(self, tmp_path: Path):
        p = tmp_path / "s.jsonl"
        _write_bytes(p, "aaaa\nbbbb\n", mode="w")
        cfg = AppConfig(seed_tail_bytes=4096)
        state = _attach(p)
        read_new(state, cfg)
        # Truncate to a smaller file (current_size < size_seen) → reset.
        _write_bytes(p, "new\n", mode="w")
        lines = read_new(state, cfg)
        assert lines == ["new"]

    def test_truncation_clears_partial_buffer(self, tmp_path: Path):
        p = tmp_path / "s.jsonl"
        _write_bytes(p, "aaaa\nbbbb\n", mode="w")
        cfg = AppConfig(seed_tail_bytes=4096)
        state = _attach(p)
        read_new(state, cfg)
        _write_bytes(p, "stale-fragment")  # buffer fills
        read_new(state, cfg)
        assert state.partial_buffer == b"stale-fragment"
        _write_bytes(p, "fresh\n", mode="w")  # truncate
        lines = read_new(state, cfg)
        assert state.partial_buffer == b""
        assert lines == ["fresh"]

    def test_inode_change_resets_deterministic(self, tmp_path: Path):
        # DETERMINISTIC inode-change reset: we do not rely on the OS handing
        # out a fresh inode (which fails to materialise on inode-reusing
        # filesystems).  Instead we force ``state.inode`` to a sentinel that
        # cannot equal the real file's inode, so the ``rotated`` branch is
        # guaranteed to fire and we can assert its effect on EVERY run.
        p = tmp_path / "s.jsonl"
        _write_bytes(p, "aaaa\nbbbb\n", mode="w")
        cfg = AppConfig(seed_tail_bytes=4096)
        state = _attach(p)
        read_new(state, cfg)
        real_inode = os.stat(p).st_ino
        # Stale, impossible inode → forces current_inode != state.inode.
        state.inode = real_inode + 1_000_000
        # Pre-fill a partial buffer so we can also prove the reset clears it.
        state.partial_buffer = b"stale-bytes"
        # Append more content (file is LARGER than size_seen, so the truncation
        # check alone would MISS this — only the inode mismatch triggers reset).
        _write_bytes(p, "rotated-a\nrotated-b\n")
        lines = read_new(state, cfg)
        # Reset re-seeded from the start (small file → all complete lines),
        # the stale partial buffer was cleared, and inode now tracks reality.
        assert "rotated-a" in lines
        assert "aaaa" in lines  # full re-read from offset 0 after reset
        assert state.partial_buffer == b""
        assert state.inode == real_inode

    def test_logrotate_replacement_rereads(self, tmp_path: Path):
        # Realistic logrotate: the original file is unlinked and replaced by a
        # LARGER new file.  We force the remembered inode to differ from the
        # replacement's so the assertion runs unconditionally (no FS-dependent
        # ``if`` guard that could silently skip the check).
        p = tmp_path / "s.jsonl"
        _write_bytes(p, "old1\nold2\n", mode="w")
        cfg = AppConfig(seed_tail_bytes=4096)
        state = _attach(p)
        read_new(state, cfg)
        # Simulate logrotate: replace the file with a larger one.
        p.unlink()
        _write_bytes(p, "rotated-a\nrotated-b\nrotated-c\nrotated-d\n", mode="w")
        new_inode = os.stat(p).st_ino
        # Guarantee a mismatch regardless of whether the OS reused the inode.
        state.inode = new_inode + 1_000_000
        lines = read_new(state, cfg)
        assert "rotated-a" in lines
        assert state.inode == new_inode


# ---------------------------------------------------------------------------
# max_line_bytes guard
# ---------------------------------------------------------------------------


class TestMaxLineBytesGuard:
    def test_overlong_line_dropped(self, tmp_path: Path):
        p = tmp_path / "s.jsonl"
        _write_bytes(p, "ok\n", mode="w")
        cfg = AppConfig(seed_tail_bytes=4096, max_line_bytes=8)
        state = _attach(p)
        read_new(state, cfg)
        _write_bytes(p, "short\n")  # 5 chars ≤ 8 → kept
        _write_bytes(p, "way-too-long-to-keep\n")  # > 8 → dropped
        _write_bytes(p, "fine\n")  # ≤ 8 → kept
        lines = read_new(state, cfg)
        assert "short" in lines
        assert "fine" in lines
        assert all(len(line) <= 8 for line in lines)

    def test_overlong_does_not_break_subsequent_lines(self, tmp_path: Path):
        p = tmp_path / "s.jsonl"
        _write_bytes(p, "x\n", mode="w")
        cfg = AppConfig(seed_tail_bytes=4096, max_line_bytes=5)
        state = _attach(p)
        read_new(state, cfg)
        _write_bytes(p, "ABCDEFGHIJ\ntiny\n")
        lines = read_new(state, cfg)
        assert lines == ["tiny"]


# ---------------------------------------------------------------------------
# Missing file tolerance
# ---------------------------------------------------------------------------


class TestMissingFileTolerance:
    def test_nonexistent_file_returns_empty(self, tmp_path: Path):
        p = tmp_path / "does-not-exist.jsonl"
        cfg = AppConfig(seed_tail_bytes=4096)
        state = _attach(p)
        assert read_new(state, cfg) == []

    def test_file_vanishing_between_reads_returns_empty(self, tmp_path: Path):
        p = tmp_path / "s.jsonl"
        _write_bytes(p, "present\n", mode="w")
        cfg = AppConfig(seed_tail_bytes=4096)
        state = _attach(p)
        assert read_new(state, cfg) == ["present"]
        p.unlink()
        assert read_new(state, cfg) == []


# ---------------------------------------------------------------------------
# Invalid UTF-8 byte-offset integrity (regression — MESSI #13, AC8)
# ---------------------------------------------------------------------------


class TestInvalidUtf8ByteOffset:
    """Byte cursor MUST advance by the true byte count, never by re-encoding.

    Claude Code transcripts embed arbitrary file / tool-output content, so a
    JSONL line can legitimately contain bytes that are not valid UTF-8.  If the
    tailer advances ``size_seen`` by re-encoding the decoded string with
    ``errors="replace"``, each bad byte becomes U+FFFD (3 bytes on re-encode)
    and ``size_seen`` runs AHEAD of the real file size.  Every later read then
    seeks past real bytes → lines silently corrupted or dropped.  These tests
    pin the byte-exact invariant and prove no data is lost.
    """

    def test_size_seen_matches_getsize_after_invalid_utf8(self, tmp_path: Path):
        # Real bytes: a clean line, a line with an invalid UTF-8 sequence
        # (\xff\xfe is not valid UTF-8), then another clean line.
        p = tmp_path / "s.jsonl"
        raw = b'{"a":1}\n' + b'{"b":"\xff\xfe"}\n' + b'{"c":3}\n'
        _write_raw_bytes(p, raw, mode="wb")
        cfg = AppConfig(seed_tail_bytes=4096, max_line_bytes=1_000_000)
        state = _attach(p)
        read_new(state, cfg)
        # The cursor must equal the real on-disk byte size — NO desync.  With
        # the text-mode re-encode bug, size_seen overshoots by 4 bytes here.
        assert state.size_seen == os.path.getsize(str(p))

    def test_clean_line_after_invalid_bytes_returned_intact(self, tmp_path: Path):
        # The clean {"c":3} line that FOLLOWS the bad line must be surfaced
        # intact (not eaten, not merged with a neighbour).
        p = tmp_path / "s.jsonl"
        raw = b'{"a":1}\n' + b'{"b":"\xff\xfe"}\n' + b'{"c":3}\n'
        _write_raw_bytes(p, raw, mode="wb")
        cfg = AppConfig(seed_tail_bytes=4096, max_line_bytes=1_000_000)
        state = _attach(p)
        lines = read_new(state, cfg)
        assert '{"c":3}' in lines
        # The clean leading line survives too, and the bad line is preserved
        # as its own (replacement-decoded) entry — nothing is dropped/merged.
        assert '{"a":1}' in lines
        assert len(lines) == 3

    def test_append_after_invalid_bytes_not_corrupted(self, tmp_path: Path):
        # The real failure mode: after a bad line desyncs the cursor, the NEXT
        # appended line is read from the WRONG offset and gets corrupted/eaten.
        # With a byte-exact cursor, the freshly appended clean line surfaces
        # whole on the next read.
        p = tmp_path / "s.jsonl"
        raw = b'{"a":1}\n' + b'{"b":"\xff\xfe"}\n'
        _write_raw_bytes(p, raw, mode="wb")
        cfg = AppConfig(seed_tail_bytes=4096, max_line_bytes=1_000_000)
        state = _attach(p)
        first = read_new(state, cfg)
        assert '{"a":1}' in first
        # Now append a brand-new clean line. A desynced cursor seeks past its
        # leading bytes; a byte-exact cursor returns it verbatim.
        _write_raw_bytes(p, b'{"d":4}\n')
        second = read_new(state, cfg)
        assert second == ['{"d":4}']
        assert state.size_seen == os.path.getsize(str(p))

    def test_invalid_bytes_split_across_two_reads_no_merge(self, tmp_path: Path):
        # Cross-boundary case: the invalid bytes (and the newline that ends
        # their line) are appended in a SECOND chunk, so the partial buffer
        # spans a read boundary.  The two JSONL records must stay distinct —
        # never merged into one — and neither may be dropped.
        p = tmp_path / "s.jsonl"
        # First read: a clean line plus the START of the bad line (no newline
        # yet → buffered as a partial fragment).
        _write_raw_bytes(p, b'{"a":1}\n{"b":"\xff', mode="wb")
        cfg = AppConfig(seed_tail_bytes=4096, max_line_bytes=1_000_000)
        state = _attach(p)
        first = read_new(state, cfg)
        assert first == ['{"a":1}']  # the partial bad line is NOT yet surfaced
        assert state.size_seen == os.path.getsize(str(p))
        # Second read: the rest of the bad line (more invalid bytes + newline)
        # plus a following clean line.
        _write_raw_bytes(p, b'\xfe"}\n{"c":3}\n')
        second = read_new(state, cfg)
        # The bad line completes as ONE record; the clean line is its own
        # record — exactly two, in order, none merged, none dropped.
        assert len(second) == 2
        assert second[1] == '{"c":3}'
        # The completed bad record stayed a single line (no newline injected,
        # no merge with {"c":3}).
        assert '{"c":3}' not in second[0]
        assert state.size_seen == os.path.getsize(str(p))


# ---------------------------------------------------------------------------
# Partial-buffer upper bound (MESSI #14 anti-unbounded-loop / OOM guard)
# ---------------------------------------------------------------------------


class TestPartialBufferBound:
    """partial_buffer must NEVER grow beyond ~max_line_bytes.

    Without an explicit cap on the accumulating partial_buffer, a single
    newline-less write (e.g. a Claude Code 'Write' tool producing a multi-MB
    JSONL line) can grow partial_buffer to the full fragment size before the
    OOM guard ever fires — because the existing guard only fires on COMPLETE
    lines (after a newline).  This test suite pins the partial-buffer bound.
    """

    def test_partial_buffer_bounded_during_accumulation(self, tmp_path: Path):
        """partial_buffer never exceeds ~max_line_bytes regardless of fragment size.

        We use a tiny cap (64 bytes) and feed a 100_000-byte newline-less
        fragment in one write.  The buffer must be capped, NOT grown to 100_000.
        After a newline arrives, the following clean line is emitted intact.
        """
        max_cap = 64
        p = tmp_path / "s.jsonl"
        # Start with a small complete line so the state is initialized.
        _write_raw_bytes(p, b"start\n", mode="wb")
        cfg = AppConfig(seed_tail_bytes=4096, max_line_bytes=max_cap)
        state = _attach(p)
        read_new(state, cfg)  # consume "start"

        # Feed a massive newline-less fragment (simulates a multi-MB Write line).
        big_fragment = b"X" * 100_000
        _write_raw_bytes(p, big_fragment)
        read_new(state, cfg)

        # BOUND: partial_buffer must NOT have grown to 100_000 bytes.
        assert len(state.partial_buffer) <= max_cap, (
            f"partial_buffer grew to {len(state.partial_buffer)} bytes — "
            f"unbounded growth, should be capped at {max_cap}"
        )

        # SIZE_SEEN: must be byte-exact (all bytes accounted for, no desync).
        expected_size = len(b"start\n") + len(big_fragment)
        assert (
            state.size_seen == expected_size
        ), f"size_seen={state.size_seen} != {expected_size}; byte cursor desynced"

    def test_size_seen_exact_across_overlong_fragment(self, tmp_path: Path):
        """size_seen advances by the exact byte count even through the overlong realign."""
        max_cap = 64
        p = tmp_path / "s.jsonl"
        _write_raw_bytes(p, b"seed\n", mode="wb")
        cfg = AppConfig(seed_tail_bytes=4096, max_line_bytes=max_cap)
        state = _attach(p)
        read_new(state, cfg)

        big_fragment = b"Y" * 100_000
        _write_raw_bytes(p, big_fragment)
        read_new(state, cfg)
        assert state.size_seen == os.path.getsize(str(p))

        # Append a newline to close the overlong line, then a clean line.
        _write_raw_bytes(p, b"\nclean_line\n")
        read_new(state, cfg)
        assert state.size_seen == os.path.getsize(str(p))

    def test_overlong_fragment_dropped_clean_line_after_newline_emitted(
        self, tmp_path: Path
    ):
        """The overlong line is NOT emitted; the next clean line after \\n IS emitted."""
        max_cap = 64
        p = tmp_path / "s.jsonl"
        _write_raw_bytes(p, b"seed\n", mode="wb")
        cfg = AppConfig(seed_tail_bytes=4096, max_line_bytes=max_cap)
        state = _attach(p)
        read_new(state, cfg)

        # Write 100_000 bytes then a newline (closes overlong line) then a clean line.
        big_fragment = b"Z" * 100_000
        _write_raw_bytes(p, big_fragment + b"\nclean_after\n")
        lines = read_new(state, cfg)

        # The overlong fragment must NOT appear.
        assert not any(
            len(line) > max_cap for line in lines
        ), f"An overlong line leaked through: {[len(n) for n in lines]}"
        # The clean line that follows the newline MUST be surfaced.
        assert (
            "clean_after" in lines
        ), f"clean_after not in lines={lines!r}; overlong realign ate subsequent line"

    def test_overlong_fragment_split_across_reads_bounded(self, tmp_path: Path):
        """Fragment fed incrementally across multiple reads stays bounded."""
        max_cap = 64
        p = tmp_path / "s.jsonl"
        _write_raw_bytes(p, b"seed\n", mode="wb")
        cfg = AppConfig(seed_tail_bytes=4096, max_line_bytes=max_cap)
        state = _attach(p)
        read_new(state, cfg)

        # Feed in 10 chunks of 500 bytes each, no newline — total 5_000 bytes.
        for _ in range(10):
            _write_raw_bytes(p, b"A" * 500)
            read_new(state, cfg)
            # After EVERY intermediate read, buffer must be bounded.
            assert (
                len(state.partial_buffer) <= max_cap
            ), f"Buffer grew to {len(state.partial_buffer)} mid-stream"

        # size_seen byte-exact after all chunks.
        assert state.size_seen == os.path.getsize(str(p))
