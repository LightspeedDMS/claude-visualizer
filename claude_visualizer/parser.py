"""JSONL line parser for Claude Code transcript files.

Two entry points share one implementation:

- :class:`EventExtractor` — the STATEFUL parser.  It carries a bounded
  ``requestId → thinking_chars`` map so it can correlate extended-thinking
  blocks to the ``tool_use`` they precede (story #3, AC3/AC4).  A live consumer
  (the pipeline) holds ONE long-lived extractor and feeds it lines in stream
  order, so a thinking block in an earlier entry enriches the ``tool_use`` in a
  later entry sharing the same top-level ``requestId``.
- :func:`parse_line` — the STATELESS convenience wrapper.  It spins up a
  throwaway extractor for a single line (so it can still see same-entry
  thinking, but never cross-entry correlation).  Retained verbatim in behaviour
  for callers and tests that parse a line in isolation.

Design notes:
- Ported from pace-maker ``_process_content_item`` nested-traversal pattern.
- Every error path returns ``[]`` — callers never see exceptions.
- Returns a list so a single line with multiple ``tool_use`` blocks yields
  multiple events (e.g. a Write + an Edit in the same assistant turn).
- The ``requestId`` map is BOUNDED (LRU capped at ``config.requestid_map_max``):
  a thinking block always PRECEDES its ``tool_use``, so only a small recent
  window of request ids is ever needed and the map can never grow for the life
  of the process (MESSI #14 anti-unbounded-loop).
"""

from __future__ import annotations

import json
import os
import re
from collections import OrderedDict
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from claude_visualizer.config import AppConfig
from claude_visualizer.events import CommandEvent, Event, FileModifiedEvent, FileOp

# Subagent path pattern: .../subagents/agent-*.jsonl
_SUBAGENT_RE = re.compile(r"[\\/]subagents[\\/]agent-[^/\\]+\.jsonl$")


def _is_subagent(source_path: str) -> bool:
    return bool(_SUBAGENT_RE.search(source_path))


def _parse_timestamp(raw: str | None) -> datetime:
    """Parse an ISO-8601 timestamp string; fall back to UTC now."""
    if not raw:
        return datetime.now(timezone.utc)
    try:
        # Replace trailing Z with +00:00 for fromisoformat compatibility
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)


def _sum_thinking_chars(content: list) -> int:
    """Total character length of every ``{type:"thinking"}`` block in content.

    A thinking block carries its text under the ``thinking`` key (verified
    schema: keys ``type``/``thinking``/``signature``).  Non-string / missing
    text contributes 0 so a malformed block never raises.
    """
    total = 0
    for item in content:
        if isinstance(item, dict) and item.get("type") == "thinking":
            text = item.get("thinking")
            if isinstance(text, str):
                total += len(text)
    return total


# Maximum display length for a single MCP input-parameter value before
# it is truncated with an ellipsis.  Keeps the generated command string
# readable before the panel's own right-truncation clips it further.
_MCP_VALUE_MAX_LEN = 60


def _format_mcp_command(tool_label: str, inp: dict) -> str:
    """Format an MCP tool call as a single-line display string.

    Produces ``Server::tool_name key=val key2=val2 …`` where each value is
    collapsed to one line and capped at ``_MCP_VALUE_MAX_LEN`` chars so the
    full string stays readable before the panel's right-truncation clips it.
    """
    parts = []
    for k, v in inp.items():
        raw = str(v)
        # Collapse newlines — tool inputs like SQL queries span many lines
        raw = " ".join(raw.split())
        if len(raw) > _MCP_VALUE_MAX_LEN:
            raw = raw[: _MCP_VALUE_MAX_LEN - 1] + "…"
        parts.append(f"{k}={raw}")
    args = " ".join(parts)
    return f"{tool_label} {args}".strip() if args else tool_label


def _process_content_item(
    item: object,
    ts: datetime,
    session_id: str,
    project_tag: str,
    source_path: str,
    is_subagent: bool,
    model: str | None,
) -> Event | None:
    """Convert a single content[] item to an Event or None.

    Ported from pace-maker ``_process_content_item``: guard ``isinstance(dict)``,
    extract type, branch on ``tool_use`` name.  Thinking enrichment is applied
    by the caller (it needs cross-entry state), so this stays a pure per-item
    mapping with no knowledge of the requestId map.
    """
    if not isinstance(item, dict):
        return None
    if item.get("type") != "tool_use":
        return None

    name = item.get("name", "")
    inp = item.get("input") or {}
    if not isinstance(inp, dict):
        inp = {}

    base = dict(
        ts=ts,
        session_id=session_id,
        is_subagent=is_subagent,
        project_tag=project_tag,
        source_path=source_path,
    )

    if name == "Write":
        return FileModifiedEvent(
            **base,  # type: ignore[arg-type]
            file_path=inp.get("file_path", ""),
            op=FileOp.WRITE,
            full_content=inp.get("content"),
            model=model,
        )

    if name == "Edit":
        return FileModifiedEvent(
            **base,  # type: ignore[arg-type]
            file_path=inp.get("file_path", ""),
            op=FileOp.EDIT,
            old_string=inp.get("old_string"),
            new_string=inp.get("new_string"),
            replace_all=inp.get("replace_all"),
            model=model,
        )

    if name == "Bash":
        return CommandEvent(
            **base,  # type: ignore[arg-type]
            command=inp.get("command", ""),
            description=inp.get("description"),
            model=model,
        )

    if name.startswith("mcp__"):
        # Strip "mcp__" prefix; first __ separator becomes ::
        tail = name[len("mcp__") :]
        server, _, tool = tail.partition("__")
        tool_label = f"{server}::{tool}" if tool else tail
        command = _format_mcp_command(tool_label, inp)
        return CommandEvent(
            **base,  # type: ignore[arg-type]
            command=command,
            model=model,
            tool_name=name,
        )

    return None


def _decode_assistant_line(
    line: str, source_path: str
) -> Optional[Tuple[list, dict, str | None]]:
    """Validate + decode an assistant JSONL line.

    Returns ``(content, base_kwargs, request_id)`` on success or ``None`` when
    the line is not a well-formed assistant entry with a list ``content``
    (every gate failure → ``None``, mirroring the "never raises" contract).
    ``base_kwargs`` holds the shared per-item fields (ts/session/project/
    source/subagent/model) so callers don't re-derive them per content item.
    """
    try:
        entry = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None

    if not isinstance(entry, dict):
        return None
    if entry.get("type") != "assistant":
        return None

    message = entry.get("message")
    if not isinstance(message, dict):
        return None

    content = message.get("content")
    if not isinstance(content, list):
        return None

    cwd: str = entry.get("cwd", "")
    request_id = entry.get("requestId")
    base = dict(
        ts=_parse_timestamp(entry.get("timestamp")),
        session_id=entry.get("sessionId", ""),
        project_tag=os.path.basename(cwd) if cwd else "",
        model=message.get("model"),
    )
    return content, base, request_id


class EventExtractor:
    """Stateful line→events parser with bounded requestId→thinking correlation.

    Hold ONE instance per live stream and feed it lines in arrival order via
    :meth:`extract`.  Extended-thinking blocks (which arrive in an entry that
    precedes the ``tool_use`` entry but share the same top-level ``requestId``)
    are remembered in a bounded LRU map and attached to the
    :class:`FileModifiedEvent` produced by the later ``tool_use``.
    """

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        # requestId -> accumulated thinking_chars, ordered oldest → newest so an
        # over-cap insert evicts the least-recently-touched request (LRU).
        self._thinking: "OrderedDict[str, int]" = OrderedDict()

    # -- introspection (tests) --------------------------------------------

    def pending_count(self) -> int:
        """Number of request ids currently holding un-consumed thinking."""
        return len(self._thinking)

    # -- core --------------------------------------------------------------

    def extract(self, line: str, source_path: str) -> List[Event]:
        """Parse one JSONL line, enriching file events with thinking state.

        Returns a (possibly empty) list of Events.  Never raises.  Steps:

        1. Decode/validate the assistant line (else ``[]``).
        2. If the entry carries any ``{type:"thinking"}`` blocks AND a
           top-level ``requestId``, record/accumulate the char count in the
           bounded map (so a later same-request ``tool_use`` can read it).
        3. For each ``tool_use`` produce its Event; for a
           :class:`FileModifiedEvent`, set ``used_thinking``/``thinking_chars``
           from the map keyed on this entry's ``requestId``.
        """
        decoded = _decode_assistant_line(line, source_path)
        if decoded is None:
            return []
        content, base, request_id = decoded
        is_subagent = _is_subagent(source_path)

        self._record_thinking(content, request_id)
        used_thinking, thinking_chars = self._peek_thinking(request_id)

        events: List[Event] = []
        consumed = False
        for item in content:
            try:
                evt = _process_content_item(
                    item,
                    base["ts"],
                    base["session_id"],
                    base["project_tag"],
                    source_path,
                    is_subagent,
                    base["model"],
                )
            except Exception:  # noqa: BLE001 (defensive per-item guard)
                continue  # pragma: no cover
            if evt is None:
                continue
            if isinstance(evt, FileModifiedEvent) and used_thinking:
                # Frozen dataclass → rebuild with the enrichment applied.
                evt = replace_thinking(evt, thinking_chars)
                consumed = True
            events.append(evt)

        # Only NOW free the pending thinking slot — and only if this entry
        # actually produced a file event for it.  A thinking-only entry (the
        # common case: thinking precedes the tool_use in a SEPARATE entry) must
        # leave its slot pending so the later same-request tool_use can read it.
        if consumed and request_id:
            self._thinking.pop(request_id, None)
        return events

    # -- internals ---------------------------------------------------------

    def _record_thinking(self, content: list, request_id: str | None) -> None:
        """Accumulate any thinking-block chars for ``request_id`` (bounded)."""
        if not request_id:
            return
        chars = _sum_thinking_chars(content)
        if chars <= 0:
            return
        # Accumulate (a request could, in principle, stream thinking in parts)
        # and mark most-recently-used by moving to the end.
        self._thinking[request_id] = self._thinking.get(request_id, 0) + chars
        self._thinking.move_to_end(request_id)
        self._evict_over_cap()

    def _peek_thinking(self, request_id: str | None) -> Tuple[bool, int]:
        """Return ``(used_thinking, thinking_chars)`` for ``request_id``.

        Non-destructive: the slot is NOT removed here.  Consumption is the
        caller's job (only once a ``FileModifiedEvent`` for this request has
        actually been produced), because the thinking block usually lives in an
        earlier, tool_use-less entry — popping on the thinking entry itself
        would erase the state before the tool_use entry could read it.
        """
        if not request_id:
            return False, 0
        chars = self._thinking.get(request_id)
        if chars is None:
            return False, 0
        return True, chars

    def _evict_over_cap(self) -> None:
        """Drop the least-recently-used request ids beyond the configured cap."""
        cap = self._config.requestid_map_max
        while len(self._thinking) > cap:
            self._thinking.popitem(last=False)


def replace_thinking(evt: FileModifiedEvent, thinking_chars: int) -> FileModifiedEvent:
    """Return a copy of ``evt`` with thinking enrichment applied.

    ``FileModifiedEvent`` is a frozen dataclass, so enrichment after
    construction means building a new instance with the two thinking fields
    flipped on.  Kept as a tiny named helper so the (verbose) field copy lives
    in one place rather than inline in the hot loop.
    """
    return FileModifiedEvent(
        ts=evt.ts,
        session_id=evt.session_id,
        is_subagent=evt.is_subagent,
        project_tag=evt.project_tag,
        source_path=evt.source_path,
        file_path=evt.file_path,
        op=evt.op,
        full_content=evt.full_content,
        old_string=evt.old_string,
        new_string=evt.new_string,
        replace_all=evt.replace_all,
        model=evt.model,
        used_thinking=True,
        thinking_chars=thinking_chars,
    )


def parse_line(line: str, source_path: str) -> List[Event]:
    """Parse one JSONL line from a Claude Code transcript (STATELESS).

    Returns a (possibly empty) list of Events.  Never raises.  This is the
    single-line convenience wrapper used by callers/tests that parse a line in
    isolation; it sees same-entry thinking but not cross-entry correlation.
    For live cross-entry correlation, hold an :class:`EventExtractor` instead.

    Gate conditions (any failure → []):
    - Line is valid JSON
    - ``entry["type"] == "assistant"``
    - ``entry["message"]`` is a dict
    - ``entry["message"]["content"]`` is a list
    """
    return EventExtractor(AppConfig()).extract(line, source_path)
