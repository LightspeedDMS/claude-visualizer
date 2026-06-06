"""Application configuration dataclass for claude-visualizer.

All tunables live here so nothing is hardcoded anywhere else.
Pass an AppConfig instance through the component graph for full testability.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class AppConfig:
    """All application tunables in one frozen, overridable object.

    Defaults reflect production values; tests construct overrides to use
    fixture paths and smaller windows.
    """

    # Root directory scanned for session transcripts.
    # Default: ~/.claude/projects (where Claude Code writes JSONL).
    projects_root: Path = field(
        default_factory=lambda: Path.home() / ".claude" / "projects"
    )

    # Files modified within this many seconds are considered "active".
    active_window_seconds: float = 120

    # Hard cap on the number of files tailed simultaneously.
    max_active_files: int = 64

    # How often the discovery scan runs (seconds).
    discovery_interval_seconds: float = 5.0

    # How often the active-file poll loop runs (seconds).
    poll_interval_seconds: float = 0.3

    # On cold-start, seek this many bytes from end to avoid full replay.
    seed_tail_bytes: int = 65_536  # 64 KB

    # Lines longer than this are dropped to prevent OOM.
    max_line_bytes: int = 1_000_000  # 1 MB

    # Maximum entries retained in the MRU model.
    mru_max: int = 50

    # --- Diff panel (story #3) ------------------------------------------
    # A rendered diff is capped at this many segment-lines; the overflow is
    # replaced by a single "…(truncated, N more lines)" footer (AC10).
    diff_max_lines: int = 500

    # The currently-displayed diff dwells at LEAST this long before the queue
    # may advance to the next file — even if its content fit on screen (AC6).
    min_dwell_seconds: float = 3.0

    # …and at MOST this long: once max dwell elapses the queue advances even if
    # the viewer has not finished scrolling a very tall diff (AC6).  Must be
    # >= min_dwell_seconds or the dwell window is empty.
    max_dwell_seconds: float = 12.0

    # Hard cap on distinct files held in the diff display queue.  On overflow
    # the stalest UNSEEN entry is dropped and surfaced as "+N more" (AC8).
    diff_queue_max: int = 32

    # Cadence at which the live UI ticks the diff queue and repaints the Diff
    # panel.  Small enough that auto-scroll (AC6) looks smooth and a newly
    # recorded file surfaces promptly; large enough to stay cheap.  This is the
    # display refresh period only — the dwell bounds above govern when the queue
    # actually advances between files.
    diff_refresh_seconds: float = 0.2

    # --- Commands feed (story #4) ---------------------------------------
    # Maximum Bash-command rows retained in the bottom Commands feed.  The feed
    # is a LOG (no dedup): every Bash tool_use across all sessions/subagents is
    # appended newest-on-top, and at this many rows the OLDEST entry falls off
    # the bottom.  Backed by a ``deque(maxlen=command_feed_max)`` so the bound
    # is structural and can never grow without limit (MESSI #14).
    command_feed_max: int = 100

    # Bound on the parser's requestId → thinking-chars correlation map.  A
    # thinking block always precedes its tool_use within the same response, so
    # only a small recent window of request ids is ever needed; the map is an
    # LRU capped here so it can never grow for the life of the process
    # (MESSI #14 anti-unbounded-loop).
    requestid_map_max: int = 256

    # Minimum seconds a user-clicked pin stays active before a new file
    # event can release it — prevents flapping on rapid activity.
    min_pin_seconds: float = 10.0

    # Cadence at which the status bar samples psutil and repaints.
    stats_refresh_seconds: float = 0.5

    # --- Persistence cache --------------------------------------------------
    # Path to the SQLite cache that persists the last-seen state across
    # restarts.  Set to None to disable caching entirely.
    cache_path: Optional[Path] = field(
        default_factory=lambda: Path.home() / ".claude-visualizer" / "cache.db"
    )

    # Maximum file events kept in the SQLite cache and in DiffQueueModel's
    # per-file event map (prevents unbounded in-memory growth).
    cache_max_file_events: int = 50

    # Maximum command events kept in the SQLite cache.
    cache_max_command_events: int = 100
