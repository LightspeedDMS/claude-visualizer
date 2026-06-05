"""Tests for the stateful, bounded ``EventExtractor`` (requestId → thinking).

The extractor is the engine-enrichment half of story #3 (AC3/AC4): a
``{type:"thinking"}`` block lives in a SEPARATE jsonl entry that PRECEDES the
``tool_use`` entry but shares the same top-level ``requestId``.  Correlation is
therefore STATEFUL across lines fed in stream order.  The map of pending
``requestId → thinking_chars`` is BOUNDED (LRU capped at
``config.requestid_map_max``) because a thinking block always precedes its
tool_use, so only a small recent window is ever needed (MESSI #14).

Anti-mock: the multi-entry stream test drives the REAL on-disk fixture
``session_thinking.jsonl`` byte-for-byte through the extractor in order.
"""

from __future__ import annotations

import json
from pathlib import Path

from claude_visualizer.config import AppConfig
from claude_visualizer.events import CommandEvent, FileModifiedEvent, FileOp
from claude_visualizer.parser import EventExtractor

FIXTURE_DIR = Path(__file__).parent / "fixtures"
THINKING_JSONL = FIXTURE_DIR / "session_thinking.jsonl"
SOURCE = "/home/user/.claude/projects/think-project/thinksess001.jsonl"


def _feed(extractor: EventExtractor, path: Path, source: str) -> list:
    """Stream every line of ``path`` through ``extractor`` IN ORDER."""
    events: list = []
    for line in path.read_text().splitlines():
        events.extend(extractor.extract(line, source))
    return events


# ---------------------------------------------------------------------------
# Real-fixture stream-order correlation (AC4)
# ---------------------------------------------------------------------------


class TestThinkingCorrelationRealFixture:
    def test_write_after_thinking_is_flagged(self):
        extractor = EventExtractor(AppConfig())
        events = _feed(extractor, THINKING_JSONL, SOURCE)
        writes = [
            e
            for e in events
            if isinstance(e, FileModifiedEvent) and e.op == FileOp.WRITE
        ]
        assert len(writes) == 1
        assert writes[0].file_path == "/home/user/think-project/app.py"
        assert writes[0].used_thinking is True
        assert writes[0].thinking_chars > 0

    def test_edit_without_thinking_is_not_flagged(self):
        extractor = EventExtractor(AppConfig())
        events = _feed(extractor, THINKING_JSONL, SOURCE)
        edits = [
            e
            for e in events
            if isinstance(e, FileModifiedEvent) and e.op == FileOp.EDIT
        ]
        assert len(edits) == 1
        assert edits[0].used_thinking is False
        assert edits[0].thinking_chars == 0

    def test_thinking_chars_matches_block_length(self):
        extractor = EventExtractor(AppConfig())
        events = _feed(extractor, THINKING_JSONL, SOURCE)
        write = next(
            e
            for e in events
            if isinstance(e, FileModifiedEvent) and e.op == FileOp.WRITE
        )
        expected = len("Let me reason about the change before writing it.")
        assert write.thinking_chars == expected

    def test_model_still_carried(self):
        extractor = EventExtractor(AppConfig())
        events = _feed(extractor, THINKING_JSONL, SOURCE)
        write = next(
            e
            for e in events
            if isinstance(e, FileModifiedEvent) and e.op == FileOp.WRITE
        )
        assert write.model == "claude-opus-4-8"


# ---------------------------------------------------------------------------
# Same-entry thinking + tool_use correlation
# ---------------------------------------------------------------------------


def _entry(request_id: str, content: list, *, model: str = "claude-opus-4-8") -> str:
    return json.dumps(
        {
            "type": "assistant",
            "timestamp": "2024-02-01T09:00:00.000Z",
            "sessionId": "sess",
            "cwd": "/home/user/proj",
            "requestId": request_id,
            "message": {"role": "assistant", "model": model, "content": content},
        }
    )


class TestThinkingCorrelationSameEntry:
    def test_thinking_and_tooluse_in_one_entry(self):
        extractor = EventExtractor(AppConfig())
        line = _entry(
            "req_same_1",
            [
                {"type": "thinking", "thinking": "abcde", "signature": "s"},
                {
                    "type": "tool_use",
                    "id": "t1",
                    "name": "Write",
                    "input": {"file_path": "/x.py", "content": "y"},
                },
            ],
        )
        events = extractor.extract(line, SOURCE)
        assert len(events) == 1
        assert events[0].used_thinking is True
        assert events[0].thinking_chars == 5

    def test_no_thinking_in_entry_means_false(self):
        extractor = EventExtractor(AppConfig())
        line = _entry(
            "req_same_2",
            [
                {
                    "type": "tool_use",
                    "id": "t1",
                    "name": "Edit",
                    "input": {
                        "file_path": "/x.py",
                        "old_string": "a",
                        "new_string": "b",
                    },
                }
            ],
        )
        events = extractor.extract(line, SOURCE)
        assert len(events) == 1
        assert events[0].used_thinking is False
        assert events[0].thinking_chars == 0

    def test_multiple_thinking_blocks_sum_chars(self):
        extractor = EventExtractor(AppConfig())
        line = _entry(
            "req_same_3",
            [
                {"type": "thinking", "thinking": "aaa", "signature": "s"},
                {"type": "thinking", "thinking": "bb", "signature": "s"},
                {
                    "type": "tool_use",
                    "id": "t1",
                    "name": "Write",
                    "input": {"file_path": "/x.py", "content": "y"},
                },
            ],
        )
        events = extractor.extract(line, SOURCE)
        assert events[0].thinking_chars == 5  # 3 + 2

    def test_thinking_without_requestid_does_not_crash(self):
        """A thinking block with no requestId cannot correlate; tool_use → False."""
        extractor = EventExtractor(AppConfig())
        line = json.dumps(
            {
                "type": "assistant",
                "timestamp": "2024-02-01T09:00:00.000Z",
                "sessionId": "sess",
                "cwd": "/home/user/proj",
                "message": {
                    "role": "assistant",
                    "model": "claude-opus-4-8",
                    "content": [
                        {"type": "thinking", "thinking": "x", "signature": "s"},
                        {
                            "type": "tool_use",
                            "id": "t1",
                            "name": "Write",
                            "input": {"file_path": "/x.py", "content": "y"},
                        },
                    ],
                },
            }
        )
        events = extractor.extract(line, SOURCE)
        assert len(events) == 1
        # No requestId to key the thinking on → cannot be attributed → False.
        assert events[0].used_thinking is False


# ---------------------------------------------------------------------------
# Bounded requestId map (MESSI #14)
# ---------------------------------------------------------------------------


class TestRequestIdMapBounded:
    def test_map_never_exceeds_cap(self):
        cfg = AppConfig(requestid_map_max=4)
        extractor = EventExtractor(cfg)
        # Feed many thinking-only entries with distinct requestIds and NO
        # following tool_use, so each one stays pending in the map.
        for i in range(50):
            extractor.extract(
                _entry(
                    f"req_{i}",
                    [{"type": "thinking", "thinking": "z", "signature": "s"}],
                ),
                SOURCE,
            )
        assert extractor.pending_count() <= cfg.requestid_map_max

    def test_oldest_requestid_evicted_when_over_cap(self):
        cfg = AppConfig(requestid_map_max=2)
        extractor = EventExtractor(cfg)
        # Record thinking for three distinct requests; cap=2 evicts the oldest.
        for rid in ("req_old", "req_mid", "req_new"):
            extractor.extract(
                _entry(rid, [{"type": "thinking", "thinking": "z", "signature": "s"}]),
                SOURCE,
            )
        # The evicted oldest request's tool_use can no longer be correlated.
        evicted = extractor.extract(
            _entry(
                "req_old",
                [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Write",
                        "input": {"file_path": "/old.py", "content": "x"},
                    }
                ],
            ),
            SOURCE,
        )
        assert evicted[0].used_thinking is False
        # …while a still-resident recent request DOES correlate.
        resident = extractor.extract(
            _entry(
                "req_new",
                [
                    {
                        "type": "tool_use",
                        "id": "t2",
                        "name": "Write",
                        "input": {"file_path": "/new.py", "content": "x"},
                    }
                ],
            ),
            SOURCE,
        )
        assert resident[0].used_thinking is True

    def test_consumed_requestid_frees_slot(self):
        """Correlating a tool_use consumes its pending thinking entry."""
        cfg = AppConfig(requestid_map_max=8)
        extractor = EventExtractor(cfg)
        extractor.extract(
            _entry("req_c", [{"type": "thinking", "thinking": "z", "signature": "s"}]),
            SOURCE,
        )
        assert extractor.pending_count() == 1
        extractor.extract(
            _entry(
                "req_c",
                [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Write",
                        "input": {"file_path": "/c.py", "content": "x"},
                    }
                ],
            ),
            SOURCE,
        )
        assert extractor.pending_count() == 0


# ---------------------------------------------------------------------------
# Parity with the stateless path for non-thinking content
# ---------------------------------------------------------------------------


class TestExtractorParity:
    def test_bash_line_still_produces_command_event(self):
        extractor = EventExtractor(AppConfig())
        line = _entry(
            "req_bash",
            [
                {
                    "type": "tool_use",
                    "id": "t1",
                    "name": "Bash",
                    "input": {"command": "ls -la", "description": "list"},
                }
            ],
        )
        events = extractor.extract(line, SOURCE)
        assert len(events) == 1
        assert isinstance(events[0], CommandEvent)
        assert events[0].command == "ls -la"

    def test_malformed_line_returns_empty(self):
        extractor = EventExtractor(AppConfig())
        assert extractor.extract("not json {{{", SOURCE) == []

    def test_two_tool_uses_one_entry_both_enriched(self):
        extractor = EventExtractor(AppConfig())
        line = _entry(
            "req_multi",
            [
                {"type": "thinking", "thinking": "abcd", "signature": "s"},
                {
                    "type": "tool_use",
                    "id": "t1",
                    "name": "Write",
                    "input": {"file_path": "/a.py", "content": "a"},
                },
                {
                    "type": "tool_use",
                    "id": "t2",
                    "name": "Edit",
                    "input": {
                        "file_path": "/b.py",
                        "old_string": "x",
                        "new_string": "y",
                    },
                },
            ],
        )
        events = extractor.extract(line, SOURCE)
        file_events = [e for e in events if isinstance(e, FileModifiedEvent)]
        assert len(file_events) == 2
        # Both tool_uses share the entry's requestId, so both see the thinking.
        assert all(e.used_thinking is True for e in file_events)
        assert all(e.thinking_chars == 4 for e in file_events)
