"""Tests for the events module - immutable event dataclasses."""

from datetime import datetime, timezone

from claude_visualizer.events import (
    CommandEvent,
    FileModifiedEvent,
    FileOp,
)

SAMPLE_TS = datetime(2024, 1, 15, 10, 30, 0, tzinfo=timezone.utc)
SAMPLE_SOURCE = "/home/user/.claude/projects/proj/sess.jsonl"


class TestFileModifiedEventWrite:
    """AC2, AC5: Write events carry correct fields and origin."""

    def test_write_event_has_base_fields(self):
        evt = FileModifiedEvent(
            ts=SAMPLE_TS,
            session_id="abc123-session",
            is_subagent=False,
            project_tag="my-project",
            source_path=SAMPLE_SOURCE,
            file_path="/home/user/project/src/main.py",
            op=FileOp.WRITE,
            full_content="print('hello')",
        )
        assert evt.ts == SAMPLE_TS
        assert evt.session_id == "abc123-session"
        assert evt.is_subagent is False
        assert evt.project_tag == "my-project"
        assert evt.source_path == SAMPLE_SOURCE

    def test_write_event_op_and_content(self):
        evt = FileModifiedEvent(
            ts=SAMPLE_TS,
            session_id="abc123",
            is_subagent=False,
            project_tag="proj",
            source_path=SAMPLE_SOURCE,
            file_path="/tmp/foo.py",
            op=FileOp.WRITE,
            full_content="content here",
        )
        assert evt.op == FileOp.WRITE
        assert evt.full_content == "content here"
        assert evt.old_string is None
        assert evt.new_string is None
        assert evt.replace_all is None

    def test_write_event_model_field(self):
        evt = FileModifiedEvent(
            ts=SAMPLE_TS,
            session_id="abc123",
            is_subagent=False,
            project_tag="proj",
            source_path=SAMPLE_SOURCE,
            file_path="/tmp/foo.py",
            op=FileOp.WRITE,
            full_content="x",
            model="claude-opus-4-8",
        )
        assert evt.model == "claude-opus-4-8"

    def test_write_event_model_defaults_none(self):
        evt = FileModifiedEvent(
            ts=SAMPLE_TS,
            session_id="abc123",
            is_subagent=False,
            project_tag="proj",
            source_path=SAMPLE_SOURCE,
            file_path="/tmp/foo.py",
            op=FileOp.WRITE,
            full_content="x",
        )
        assert evt.model is None


class TestFileModifiedEventThinking:
    """AC3/AC4: thinking enrichment fields default off and carry when set."""

    def _evt(self, **overrides):
        base = dict(
            ts=SAMPLE_TS,
            session_id="abc123",
            is_subagent=False,
            project_tag="proj",
            source_path=SAMPLE_SOURCE,
            file_path="/tmp/foo.py",
            op=FileOp.WRITE,
            full_content="x",
        )
        base.update(overrides)
        return FileModifiedEvent(**base)

    def test_used_thinking_defaults_false(self):
        assert self._evt().used_thinking is False

    def test_thinking_chars_defaults_zero(self):
        assert self._evt().thinking_chars == 0

    def test_used_thinking_settable_true(self):
        assert self._evt(used_thinking=True).used_thinking is True

    def test_thinking_chars_settable(self):
        assert self._evt(thinking_chars=1234).thinking_chars == 1234


class TestFileModifiedEventEdit:
    """AC2, AC5: Edit events carry old/new strings."""

    def test_edit_event_fields(self):
        evt = FileModifiedEvent(
            ts=SAMPLE_TS,
            session_id="sess-edit",
            is_subagent=False,
            project_tag="proj",
            source_path=SAMPLE_SOURCE,
            file_path="/tmp/bar.py",
            op=FileOp.EDIT,
            old_string="old code",
            new_string="new code",
            replace_all=False,
        )
        assert evt.op == FileOp.EDIT
        assert evt.old_string == "old code"
        assert evt.new_string == "new code"
        assert evt.replace_all is False
        assert evt.full_content is None

    def test_edit_replace_all(self):
        evt = FileModifiedEvent(
            ts=SAMPLE_TS,
            session_id="sess",
            is_subagent=False,
            project_tag="proj",
            source_path=SAMPLE_SOURCE,
            file_path="/tmp/baz.py",
            op=FileOp.EDIT,
            old_string="foo",
            new_string="bar",
            replace_all=True,
        )
        assert evt.replace_all is True


class TestCommandEvent:
    """AC2: CommandEvent carries command + description."""

    def test_command_event_fields(self):
        evt = CommandEvent(
            ts=SAMPLE_TS,
            session_id="sess-bash",
            is_subagent=False,
            project_tag="my-proj",
            source_path=SAMPLE_SOURCE,
            command="ls -la",
            description="List files",
            model="claude-sonnet-4",
        )
        assert evt.command == "ls -la"
        assert evt.description == "List files"
        assert evt.model == "claude-sonnet-4"
        assert evt.session_id == "sess-bash"

    def test_command_event_model_optional(self):
        evt = CommandEvent(
            ts=SAMPLE_TS,
            session_id="sess",
            is_subagent=True,
            project_tag="proj",
            source_path=SAMPLE_SOURCE,
            command="git status",
            description=None,
        )
        assert evt.model is None
        assert evt.is_subagent is True
        assert evt.description is None


class TestSubagentMarker:
    """AC5: subagent events have is_subagent=True."""

    def test_subagent_write_event(self):
        evt = FileModifiedEvent(
            ts=SAMPLE_TS,
            session_id="sess-sub",
            is_subagent=True,
            project_tag="my-proj",
            source_path="/home/user/.claude/projects/proj/sess/subagents/agent-abc.jsonl",
            file_path="/tmp/file.py",
            op=FileOp.WRITE,
            full_content="x",
        )
        assert evt.is_subagent is True


class TestFileOpEnum:
    """FileOp enum has WRITE and EDIT."""

    def test_file_op_values(self):
        assert FileOp.WRITE.value == "WRITE"
        assert FileOp.EDIT.value == "EDIT"

    def test_file_op_comparison(self):
        assert FileOp.WRITE != FileOp.EDIT
