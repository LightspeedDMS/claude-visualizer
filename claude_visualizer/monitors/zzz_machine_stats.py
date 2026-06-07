"""Bundled machine-stats monitor — CPU / RAM / Disk IO / Net IO status bar.

This module is the RELOCATED home of the system-resource rendering logic that
previously lived in ``ui/panels.py``.  It exposes the identical public API so
the 12 existing ``test_system_stats.py`` tests and the 13 ``TestRenderStatusBar``
/ ``TestFmtRate`` tests in ``test_ui.py`` remain unchanged — only the import
path shifts from ``claude_visualizer.ui.panels`` to
``claude_visualizer.monitors.zzz_machine_stats`` (AC4).

``Monitor.tick(now)`` returns the same ``render_status_bar`` output so the
bar line is byte-identical to the pre-refactor status bar.

The ``zzz_`` prefix ensures this file sorts alphabetically last when the
registry loads monitors, so user-added monitors appear before the built-in
machine stats.
"""

from __future__ import annotations

from rich.text import Text

from claude_visualizer.models.system_stats import SystemStatsModel, SystemStatsSnapshot

# ---------------------------------------------------------------------------
# Constants (relocated from ui/panels.py)
# ---------------------------------------------------------------------------

_STATS_BAR_WIDTH = 12
_STATS_BAR_FILL = "█"
_STATS_BAR_EMPTY = "░"
_STATS_GREEN = "green"
_STATS_YELLOW = "yellow"
_STATS_RED = "red"


# ---------------------------------------------------------------------------
# Pure helpers (relocated from ui/panels.py)
# ---------------------------------------------------------------------------


def _bar_colour(pct: float) -> str:
    if pct >= 80:
        return _STATS_RED
    if pct >= 60:
        return _STATS_YELLOW
    return _STATS_GREEN


def _fmt_rate(bps: float) -> str:
    """Format a byte rate as a 7-char right-padded human-readable string."""
    if bps < 1024:
        s = f"{int(bps)}B/s"
    elif bps < 1024**2:
        val = bps / 1024
        s = f"{val:.1f}K/s" if val < 100 else f"{val:.0f}K/s"
    elif bps < 1024**3:
        val = bps / 1024**2
        s = f"{val:.1f}M/s" if val < 100 else f"{val:.0f}M/s"
    else:
        val = bps / 1024**3
        s = f"{val:.1f}G/s" if val < 100 else f"{val:.0f}G/s"
    return s.ljust(7)


def render_status_bar(snapshot: SystemStatsSnapshot) -> Text:
    """Render the one-line system-stats bar (Design B).

    Output is IDENTICAL to the pre-refactor ``panels.render_status_bar``
    so AC4 is satisfied — existing tests prove the colours and content
    are unchanged.
    """
    out = Text(no_wrap=True, overflow="ellipsis")

    def _bar(pct: float) -> None:
        filled = min(_STATS_BAR_WIDTH, round(_STATS_BAR_WIDTH * pct / 100))
        out.append(
            _STATS_BAR_FILL * filled + _STATS_BAR_EMPTY * (_STATS_BAR_WIDTH - filled),
            style=_bar_colour(pct),
        )

    out.append(" CPU ")
    _bar(snapshot.cpu_pct)
    out.append(f" {int(snapshot.cpu_pct)}%")

    out.append(" │ ", style="dim")
    out.append("RAM ")
    _bar(snapshot.ram_pct)
    out.append(f" {int(snapshot.ram_pct)}%")

    free = snapshot.ram_free_bytes
    if free >= 1024**3:
        out.append(f"  {free / 1024 ** 3:.1f}G free")
    else:
        out.append(f"  {free / 1024 ** 2:.0f}M free")

    out.append(" │ ", style="dim")
    out.append(
        f"Disk r:{_fmt_rate(snapshot.disk_read_bps)}"
        f" w:{_fmt_rate(snapshot.disk_write_bps)}"
    )
    out.append(" │ ", style="dim")
    out.append(
        f"Net ↓{_fmt_rate(snapshot.net_down_bps)}" f" ↑{_fmt_rate(snapshot.net_up_bps)}"
    )

    return out


# ---------------------------------------------------------------------------
# Monitor class — entry point for MonitorRegistry
# ---------------------------------------------------------------------------


class Monitor:
    """Bundled machine-stats monitor: wraps SystemStatsModel for the registry.

    ``tick(now)`` returns the same ``Text`` object as the pre-refactor
    ``StatusBar.update_from_snapshot``  — byte-identical output (AC4).
    """

    def __init__(self) -> None:
        self._model = SystemStatsModel()

    def tick(self, now: float) -> Text:
        """Sample system stats and return the rendered status-bar line."""
        return render_status_bar(self._model.tick(now))
