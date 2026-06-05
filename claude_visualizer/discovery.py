"""Transcript file discovery and active-set selection.

Two pure functions drive the watch loop:

- :func:`discover` walks ``projects_root`` and returns every Claude Code
  transcript (``*.jsonl``), including subagent transcripts living under
  ``*/subagents/agent-*.jsonl``.  ``*.meta.json`` sidecars are excluded.

- :func:`active_set` narrows a candidate list down to files that were
  modified recently (within ``config.active_window_seconds``), ordered
  newest-first and capped at ``config.max_active_files`` so we never tail an
  unbounded number of files.

Both functions are tolerant: a file that vanishes or denies permission
between the directory scan and the ``stat`` call is silently skipped rather
than raising — this is the documented behaviour for a live, churning
``~/.claude/projects`` tree, NOT a blanket error-swallow.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import List, Tuple, Union

PathLike = Union[str, Path]


def discover(projects_root: PathLike) -> List[str]:
    """Return absolute paths of all ``*.jsonl`` transcripts under root.

    Recursion naturally includes ``*/subagents/agent-*.jsonl``.  Non-jsonl
    files (including ``*.meta.json`` sidecars) are excluded.  A missing root
    yields an empty list.  The result is sorted for determinism.
    """
    root = Path(projects_root)
    if not root.exists():
        return []

    results: List[str] = []
    # rglob("*.jsonl") matches only names ending in ".jsonl", so ".meta.json"
    # sidecars are excluded by construction.  We still skip non-files because
    # a *directory* could be named "<something>.jsonl".
    for path in root.rglob("*.jsonl"):
        try:
            if not path.is_file():
                continue
        except OSError:
            # Vanished/permission-denied during the walk — skip this entry.
            continue
        results.append(str(path.resolve()))

    results.sort()
    return results


def active_set(paths: List[str], config) -> List[str]:
    """Select recently-modified files, newest-first, capped at the max.

    A file is "active" when ``now - mtime <= config.active_window_seconds``.
    Files that cannot be stat'd (vanished, permission denied) are skipped.
    The active files are sorted by mtime descending and truncated to
    ``config.max_active_files``.
    """
    now = time.time()
    window = config.active_window_seconds

    active: List[Tuple[float, str]] = []
    for path in paths:
        try:
            mtime = os.stat(path).st_mtime
        except (FileNotFoundError, PermissionError, OSError):
            continue
        if now - mtime <= window:
            active.append((mtime, path))

    # Newest first; tie-break by path for determinism.
    active.sort(key=lambda item: (-item[0], item[1]))

    capped = active[: config.max_active_files]
    return [path for _, path in capped]
