"""Tests for parser.parse_line() — the core JSONL line-to-event converter."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from claude_visualizer.events import CommandEvent, FileModifiedEvent, FileOp
from claude_visualizer.parser import parse_line

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FIXTURE_DIR = Path(__file__).parent / "fixtures"
NORMAL_JSONL = FIXTURE_DIR / "session_normal.jsonl"
MALFORMED_JSONL = FIXTURE_DIR / "session_malformed.jsonl"

NORMAL_SOURCE = "/home/user/.claude/projects/my-project/abc123def456.jsonl"
SUBAGENT_SOURCE = (
    "/home/user/.claude/projects/my-project"
    "/abc123def456/subagents/agent-xyz789.jsonl"
)


def _make_write_line(
    file_path: str = "/tmp/foo.py",
    content: str = "x",
    session_id: str = "sess01",
    model: str = "claude-opus-4-5",
    cwd: str = "/home/user/my-project",
) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "timestamp": "2024-01-15T10:00:00.000Z",
            "sessionId": session_id,
            "cwd": cwd,
            "message": {
                "role": "assistant",
                "model": model,
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_w",
                        "name": "Write",
                        "input": {"file_path": file_path, "content": content},
                    }
                ],
            },
        }
    )


def _make_edit_line(
    file_path: str = "/tmp/bar.py",
    old_string: str = "old",
    new_string: str = "new",
    replace_all: bool = False,
    session_id: str = "sess01",
    model: str = "claude-opus-4-5",
    cwd: str = "/home/user/my-project",
) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "timestamp": "2024-01-15T10:00:01.000Z",
            "sessionId": session_id,
            "cwd": cwd,
            "message": {
                "role": "assistant",
                "model": model,
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_e",
                        "name": "Edit",
                        "input": {
                            "file_path": file_path,
                            "old_string": old_string,
                            "new_string": new_string,
                            "replace_all": replace_all,
                        },
                    }
                ],
            },
        }
    )


def _make_bash_line(
    command: str = "ls -la",
    description: str | None = "List files",
    session_id: str = "sess01",
    model: str = "claude-opus-4-5",
    cwd: str = "/home/user/my-project",
) -> str:
    inp: dict = {"command": command}
    if description is not None:
        inp["description"] = description
    return json.dumps(
        {
            "type": "assistant",
            "timestamp": "2024-01-15T10:00:02.000Z",
            "sessionId": session_id,
            "cwd": cwd,
            "message": {
                "role": "assistant",
                "model": model,
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_b",
                        "name": "Bash",
                        "input": inp,
                    }
                ],
            },
        }
    )


# ---------------------------------------------------------------------------
# Write tool_use
# ---------------------------------------------------------------------------


class TestParseLineWrite:
    """AC2: Write tool_use → FileModifiedEvent(op=WRITE)."""

    def test_write_returns_list(self):
        result = parse_line(_make_write_line(), NORMAL_SOURCE)
        assert isinstance(result, list)

    def test_write_single_event(self):
        events = parse_line(_make_write_line(), NORMAL_SOURCE)
        assert len(events) == 1

    def test_write_event_type(self):
        events = parse_line(_make_write_line(), NORMAL_SOURCE)
        assert isinstance(events[0], FileModifiedEvent)

    def test_write_op(self):
        events = parse_line(_make_write_line(), NORMAL_SOURCE)
        assert events[0].op == FileOp.WRITE

    def test_write_file_path(self):
        events = parse_line(
            _make_write_line(file_path="/tmp/my_file.py"), NORMAL_SOURCE
        )
        assert events[0].file_path == "/tmp/my_file.py"

    def test_write_full_content(self):
        events = parse_line(_make_write_line(content="hello"), NORMAL_SOURCE)
        assert events[0].full_content == "hello"

    def test_write_edit_fields_none(self):
        events = parse_line(_make_write_line(), NORMAL_SOURCE)
        evt = events[0]
        assert evt.old_string is None
        assert evt.new_string is None
        assert evt.replace_all is None

    def test_write_model(self):
        events = parse_line(_make_write_line(model="claude-opus-4-5"), NORMAL_SOURCE)
        assert events[0].model == "claude-opus-4-5"

    def test_write_session_id(self):
        events = parse_line(_make_write_line(session_id="mysession"), NORMAL_SOURCE)
        assert events[0].session_id == "mysession"

    def test_write_project_tag_from_cwd_basename(self):
        events = parse_line(
            _make_write_line(cwd="/home/user/my-project"), NORMAL_SOURCE
        )
        assert events[0].project_tag == "my-project"

    def test_write_source_path(self):
        events = parse_line(_make_write_line(), NORMAL_SOURCE)
        assert events[0].source_path == NORMAL_SOURCE

    def test_write_not_subagent(self):
        events = parse_line(_make_write_line(), NORMAL_SOURCE)
        assert events[0].is_subagent is False

    def test_write_timestamp_parsed(self):
        events = parse_line(_make_write_line(), NORMAL_SOURCE)
        assert isinstance(events[0].ts, datetime)


# ---------------------------------------------------------------------------
# Edit tool_use
# ---------------------------------------------------------------------------


class TestParseLineEdit:
    """AC2: Edit tool_use → FileModifiedEvent(op=EDIT)."""

    def test_edit_op(self):
        events = parse_line(_make_edit_line(), NORMAL_SOURCE)
        assert events[0].op == FileOp.EDIT

    def test_edit_old_string(self):
        events = parse_line(_make_edit_line(old_string="foo"), NORMAL_SOURCE)
        assert events[0].old_string == "foo"

    def test_edit_new_string(self):
        events = parse_line(_make_edit_line(new_string="bar"), NORMAL_SOURCE)
        assert events[0].new_string == "bar"

    def test_edit_replace_all_false(self):
        events = parse_line(_make_edit_line(replace_all=False), NORMAL_SOURCE)
        assert events[0].replace_all is False

    def test_edit_replace_all_true(self):
        events = parse_line(_make_edit_line(replace_all=True), NORMAL_SOURCE)
        assert events[0].replace_all is True

    def test_edit_full_content_none(self):
        events = parse_line(_make_edit_line(), NORMAL_SOURCE)
        assert events[0].full_content is None


# ---------------------------------------------------------------------------
# Bash tool_use
# ---------------------------------------------------------------------------


class TestParseLineBash:
    """AC2: Bash tool_use → CommandEvent."""

    def test_bash_returns_command_event(self):
        events = parse_line(_make_bash_line(), NORMAL_SOURCE)
        assert len(events) == 1
        assert isinstance(events[0], CommandEvent)

    def test_bash_command(self):
        events = parse_line(_make_bash_line(command="git status"), NORMAL_SOURCE)
        assert events[0].command == "git status"

    def test_bash_description(self):
        events = parse_line(_make_bash_line(description="Check git"), NORMAL_SOURCE)
        assert events[0].description == "Check git"

    def test_bash_description_optional(self):
        events = parse_line(_make_bash_line(description=None), NORMAL_SOURCE)
        assert events[0].description is None

    def test_bash_model(self):
        events = parse_line(_make_bash_line(model="claude-sonnet-4-6"), NORMAL_SOURCE)
        assert events[0].model == "claude-sonnet-4-6"


# ---------------------------------------------------------------------------
# Subagent detection
# ---------------------------------------------------------------------------


class TestSubagentDetection:
    """AC5: is_subagent=True when source path matches subagents/agent-*.jsonl."""

    def test_subagent_path_sets_flag(self):
        events = parse_line(_make_write_line(), SUBAGENT_SOURCE)
        assert events[0].is_subagent is True

    def test_normal_path_not_subagent(self):
        events = parse_line(_make_write_line(), NORMAL_SOURCE)
        assert events[0].is_subagent is False

    def test_subagent_fixture_path(self):
        subagent_src = (
            "/home/user/.claude/projects/proj/sess/subagents/agent-abc123.jsonl"
        )
        events = parse_line(_make_write_line(), subagent_src)
        assert events[0].is_subagent is True


# ---------------------------------------------------------------------------
# Multi tool_use in one line
# ---------------------------------------------------------------------------


class TestMultiToolUse:
    """AC2: A single line with multiple tool_use blocks yields multiple events."""

    def test_two_writes_yield_two_events(self):
        line = json.dumps(
            {
                "type": "assistant",
                "timestamp": "2024-01-15T10:00:00.000Z",
                "sessionId": "sess01",
                "cwd": "/home/user/proj",
                "message": {
                    "role": "assistant",
                    "model": "claude-opus-4-5",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "t1",
                            "name": "Write",
                            "input": {"file_path": "/a.py", "content": "a"},
                        },
                        {
                            "type": "tool_use",
                            "id": "t2",
                            "name": "Write",
                            "input": {"file_path": "/b.py", "content": "b"},
                        },
                    ],
                },
            }
        )
        events = parse_line(line, NORMAL_SOURCE)
        assert len(events) == 2
        paths = {e.file_path for e in events}
        assert paths == {"/a.py", "/b.py"}

    def test_write_and_bash_in_one_line(self):
        line = json.dumps(
            {
                "type": "assistant",
                "timestamp": "2024-01-15T10:00:00.000Z",
                "sessionId": "sess01",
                "cwd": "/home/user/proj",
                "message": {
                    "role": "assistant",
                    "model": "claude-opus-4-5",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "t1",
                            "name": "Write",
                            "input": {"file_path": "/a.py", "content": "a"},
                        },
                        {
                            "type": "tool_use",
                            "id": "t2",
                            "name": "Bash",
                            "input": {"command": "ls"},
                        },
                    ],
                },
            }
        )
        events = parse_line(line, NORMAL_SOURCE)
        assert len(events) == 2
        types = {type(e) for e in events}
        assert FileModifiedEvent in types
        assert CommandEvent in types


# ---------------------------------------------------------------------------
# Malformed / non-event lines → empty list (AC6)
# ---------------------------------------------------------------------------


class TestMalformedLines:
    """AC6: Per-line corruption → empty list, never raise."""

    def test_bad_json_returns_empty(self):
        assert parse_line("not json {{{{", NORMAL_SOURCE) == []

    def test_empty_string_returns_empty(self):
        assert parse_line("", NORMAL_SOURCE) == []

    def test_user_type_returns_empty(self):
        line = json.dumps({"type": "user", "message": {"role": "user", "content": []}})
        assert parse_line(line, NORMAL_SOURCE) == []

    def test_system_type_returns_empty(self):
        line = json.dumps({"type": "system", "sessionId": "x", "cwd": "/proj"})
        assert parse_line(line, NORMAL_SOURCE) == []

    def test_content_not_list_returns_empty(self):
        line = json.dumps(
            {
                "type": "assistant",
                "sessionId": "sess",
                "cwd": "/proj",
                "message": {
                    "role": "assistant",
                    "content": "not a list",
                },
            }
        )
        assert parse_line(line, NORMAL_SOURCE) == []

    def test_message_not_dict_returns_empty(self):
        line = json.dumps(
            {
                "type": "assistant",
                "sessionId": "sess",
                "cwd": "/proj",
                "message": "not a dict",
            }
        )
        assert parse_line(line, NORMAL_SOURCE) == []

    def test_unknown_tool_returns_empty(self):
        line = json.dumps(
            {
                "type": "assistant",
                "timestamp": "2024-01-15T10:00:00.000Z",
                "sessionId": "sess",
                "cwd": "/proj",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "t1",
                            "name": "Read",
                            "input": {"file_path": "/x.py"},
                        }
                    ],
                },
            }
        )
        assert parse_line(line, NORMAL_SOURCE) == []

    def test_non_tool_use_content_items_skipped(self):
        """text and thinking blocks in content do not produce events."""
        line = json.dumps(
            {
                "type": "assistant",
                "timestamp": "2024-01-15T10:00:00.000Z",
                "sessionId": "sess",
                "cwd": "/proj",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "I will do things"},
                        {"type": "thinking", "thinking": "..."},
                    ],
                },
            }
        )
        assert parse_line(line, NORMAL_SOURCE) == []

    def test_malformed_fixture_file(self):
        """Full fixture file: only the 2 good Write lines produce events."""
        source = str(MALFORMED_JSONL)
        events_all = []
        for line in MALFORMED_JSONL.read_text().splitlines():
            events_all.extend(parse_line(line, source))
        # Two Write tool_use in malformed.jsonl are the only valid events
        assert len(events_all) == 2
        paths = {e.file_path for e in events_all}
        assert "/tmp/good.py" in paths
        assert "/tmp/also_good.py" in paths


# ---------------------------------------------------------------------------
# Normal fixture file — exhaustive event check
# ---------------------------------------------------------------------------


class TestNormalFixture:
    """Parse the full session_normal.jsonl and assert exact event stream."""

    def _parse_fixture(self, path: Path, source: str) -> list:
        events = []
        for line in path.read_text().splitlines():
            events.extend(parse_line(line, source))
        return events

    def test_normal_fixture_event_count(self):
        events = self._parse_fixture(NORMAL_JSONL, NORMAL_SOURCE)
        # line 2: Write; line 4: Edit; line 6: Bash; line 8: Write + Edit = 5 total
        assert len(events) == 5

    def test_normal_fixture_first_event_write(self):
        events = self._parse_fixture(NORMAL_JSONL, NORMAL_SOURCE)
        assert isinstance(events[0], FileModifiedEvent)
        assert events[0].op == FileOp.WRITE
        assert events[0].file_path == "/home/user/my-project/src/main.py"

    def test_normal_fixture_second_event_edit(self):
        events = self._parse_fixture(NORMAL_JSONL, NORMAL_SOURCE)
        assert isinstance(events[1], FileModifiedEvent)
        assert events[1].op == FileOp.EDIT

    def test_normal_fixture_third_event_bash(self):
        events = self._parse_fixture(NORMAL_JSONL, NORMAL_SOURCE)
        assert isinstance(events[2], CommandEvent)
        assert events[2].command == "python src/main.py"

    def test_normal_fixture_project_tag(self):
        events = self._parse_fixture(NORMAL_JSONL, NORMAL_SOURCE)
        assert all(e.project_tag == "my-project" for e in events)


# ---------------------------------------------------------------------------
# Defensive parser branches (timestamp fallbacks, non-dict items/inputs,
# non-dict top-level JSON) — guarantee the "never raises" contract holds and
# every error path returns [].
# ---------------------------------------------------------------------------


class TestParserDefensiveBranches:
    def test_missing_timestamp_falls_back_to_now(self):
        """No timestamp field → ts is populated with a UTC datetime (not crash)."""
        line = json.dumps(
            {
                "type": "assistant",
                "sessionId": "sess",
                "cwd": "/proj",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "t1",
                            "name": "Write",
                            "input": {"file_path": "/a.py", "content": "x"},
                        }
                    ],
                },
            }
        )
        events = parse_line(line, NORMAL_SOURCE)
        assert len(events) == 1
        assert isinstance(events[0].ts, datetime)

    def test_malformed_timestamp_falls_back_to_now(self):
        """A non-ISO timestamp string must not raise; ts falls back to now."""
        line = json.dumps(
            {
                "type": "assistant",
                "timestamp": "not-a-real-timestamp",
                "sessionId": "sess",
                "cwd": "/proj",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "t1",
                            "name": "Write",
                            "input": {"file_path": "/a.py", "content": "x"},
                        }
                    ],
                },
            }
        )
        before = datetime.now(timezone.utc)
        events = parse_line(line, NORMAL_SOURCE)
        assert len(events) == 1
        assert events[0].ts >= before

    def test_non_dict_content_item_skipped(self):
        """A bare string/number in content[] is not a tool_use → skipped."""
        line = json.dumps(
            {
                "type": "assistant",
                "timestamp": "2024-01-15T10:00:00.000Z",
                "sessionId": "sess",
                "cwd": "/proj",
                "message": {
                    "role": "assistant",
                    "content": [
                        "a bare string item",
                        42,
                        {
                            "type": "tool_use",
                            "id": "t1",
                            "name": "Write",
                            "input": {"file_path": "/keep.py", "content": "x"},
                        },
                    ],
                },
            }
        )
        events = parse_line(line, NORMAL_SOURCE)
        # Only the real tool_use survives; the bare items are ignored.
        assert len(events) == 1
        assert events[0].file_path == "/keep.py"

    def test_tool_use_with_non_dict_input_defaults_empty(self):
        """input that is a list (not a dict) → treated as {} → empty fields."""
        line = json.dumps(
            {
                "type": "assistant",
                "timestamp": "2024-01-15T10:00:00.000Z",
                "sessionId": "sess",
                "cwd": "/proj",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "t1",
                            "name": "Write",
                            "input": ["unexpected", "list"],
                        }
                    ],
                },
            }
        )
        events = parse_line(line, NORMAL_SOURCE)
        assert len(events) == 1
        # Defaulted to {} so file_path is the empty-string default.
        assert events[0].file_path == ""
        assert events[0].full_content is None

    def test_top_level_json_list_returns_empty(self):
        """A JSON array at the top level is not a dict entry → []."""
        assert parse_line(json.dumps([1, 2, 3]), NORMAL_SOURCE) == []

    def test_top_level_json_number_returns_empty(self):
        """A bare JSON number at the top level → []."""
        assert parse_line("42", NORMAL_SOURCE) == []

    def test_top_level_json_null_returns_empty(self):
        """A JSON null at the top level → []."""
        assert parse_line("null", NORMAL_SOURCE) == []


# ---------------------------------------------------------------------------
# MCP tool_use → CommandEvent (new in story #5)
# ---------------------------------------------------------------------------


def _make_mcp_line(
    tool_name: str = "mcp__MyServer__search_code",
    inp: dict | None = None,
    session_id: str = "sess01",
    model: str = "claude-opus-4-5",
    cwd: str = "/home/user/my-project",
) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "timestamp": "2024-01-15T10:00:03.000Z",
            "sessionId": session_id,
            "cwd": cwd,
            "message": {
                "role": "assistant",
                "model": model,
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_m",
                        "name": tool_name,
                        "input": (
                            inp if inp is not None else {"query": "auth", "limit": 5}
                        ),
                    }
                ],
            },
        }
    )


class TestMcpToolUse:
    """MCP tool_use → CommandEvent with tool_name field set."""

    def test_mcp_tool_emits_command_event(self):
        events = parse_line(
            _make_mcp_line(
                tool_name="mcp__MyServer__search_code",
                inp={"query": "auth", "limit": 5},
            ),
            NORMAL_SOURCE,
        )
        assert len(events) == 1
        assert isinstance(events[0], CommandEvent)
        assert events[0].tool_name == "mcp__MyServer__search_code"
        assert events[0].command.startswith("MyServer::search_code")
        assert "query=auth" in events[0].command

    def test_mcp_tool_multiline_value_collapsed(self):
        events = parse_line(
            _make_mcp_line(
                tool_name="mcp__DB__run_query",
                inp={"sql": "SELECT *\nFROM foo\nWHERE id=1"},
            ),
            NORMAL_SOURCE,
        )
        assert len(events) == 1
        assert "\n" not in events[0].command

    def test_mcp_tool_value_truncated_at_60(self):
        long_val = "x" * 80  # 80 chars — exceeds 60-char cap
        events = parse_line(
            _make_mcp_line(
                tool_name="mcp__Server__do_thing",
                inp={"param": long_val},
            ),
            NORMAL_SOURCE,
        )
        assert len(events) == 1
        # The value portion in the command must end with '…'
        assert "…" in events[0].command
        # The param= value should be capped
        param_part = events[0].command.split("param=", 1)[1]
        assert param_part.endswith("…")
        assert len(param_part) <= 61  # 60 chars + ellipsis

    def test_bash_tool_name_unchanged(self):
        events = parse_line(_make_bash_line(command="git status"), NORMAL_SOURCE)
        assert len(events) == 1
        assert isinstance(events[0], CommandEvent)
        assert events[0].tool_name == "Bash"
