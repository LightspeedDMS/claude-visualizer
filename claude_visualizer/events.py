"""Immutable event dataclasses for the claude-visualizer pipeline.

Events flow from parser → queue → dispatcher → panel models.
All events are arrival-ordered (not globally timestamp-sorted).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional


class FileOp(Enum):
    """Operation type for file-modifying tool calls."""

    WRITE = "WRITE"
    EDIT = "EDIT"


@dataclass(frozen=True)
class Event:
    """Base event with origin metadata."""

    ts: datetime
    session_id: str
    is_subagent: bool
    project_tag: str
    source_path: str


@dataclass(frozen=True)
class FileModifiedEvent(Event):
    """Emitted when a Write or Edit tool_use block is parsed.

    WRITE: full_content is set; old_string/new_string/replace_all are None.
    EDIT:  old_string/new_string/replace_all are set; full_content is None.
    """

    file_path: str = ""
    op: FileOp = FileOp.WRITE
    # WRITE fields
    full_content: Optional[str] = None
    # EDIT fields
    old_string: Optional[str] = None
    new_string: Optional[str] = None
    replace_all: Optional[bool] = None
    # Optional enrichment
    model: Optional[str] = None
    # Story #3 thinking correlation (populated by the parser's EventExtractor):
    # True iff this tool_use's response (shared requestId) contained a
    # {type:"thinking"} block.  thinking_chars is the total length of that
    # thinking text (0 when absent).  Defaults are off/0 so an un-enriched
    # event reads as "no thinking" rather than ambiguous None.
    used_thinking: bool = False
    thinking_chars: int = 0


@dataclass(frozen=True)
class CommandEvent(Event):
    """Emitted when a Bash or MCP tool_use block is parsed.

    Emitted here so story #4 (Commands Feed) is a pure consumer
    with no parser changes required.

    ``tool_name`` identifies the originating tool: ``"Bash"`` for shell
    commands; the full ``mcp__<server>__<tool>`` name for MCP calls.  The
    ``"Bash"`` default preserves all existing callers unchanged.
    """

    command: str = ""
    description: Optional[str] = None
    model: Optional[str] = None
    tool_name: str = "Bash"
