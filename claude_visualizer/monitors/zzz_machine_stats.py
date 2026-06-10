"""Bundled machine-stats monitor — CPU / RAM / Disk IO / Net IO status bar.

This module is the RELOCATED home of the system-resource rendering logic that
previously lived in ``ui/panels.py``.  It exposes the identical public API so
the 12 existing ``test_system_stats.py`` model tests remain unchanged.
The ``TestRenderStatusBar`` / ``TestFmtRate`` tests in ``test_ui.py`` were
updated to assert the fixed-width column layout described below, and a
column-stability test was added to guard against horizontal jitter.

``render_status_bar`` uses **fixed-width** numeric fields so the status bar
columns never jitter horizontally as values change digit-count:

  - CPU %  is right-aligned to 3 chars  (``f"{pct:>3}%"`` → constant 4-char slot)
  - RAM %  is right-aligned to 3 chars  (same)
  - free RAM token is right-aligned to 6 chars before `` free``
    (covers ``0M`` … ``1023M`` and ``1.0G`` … ``999.9G``; ≥1 TB → ``1024.0G``
    overflows to 7 chars but that is an extraordinary edge case)

Disk/Net rates were already fixed-width via ``_fmt_rate(...).ljust(7)`` and
are unchanged.

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

# Volume free-space rotation: advance to the next mount every 10 seconds.
_VOLUME_ROTATE_SECONDS = 10.0

# Color thresholds for free-space percentage (free < X → color).
_VOLUME_RED_THRESHOLD = 10.0  # < 10% free → red
_VOLUME_YELLOW_THRESHOLD = 25.0  # < 25% free → yellow; else green


# ---------------------------------------------------------------------------
# Pure helpers (relocated from ui/panels.py)
# ---------------------------------------------------------------------------


def _bar_colour(pct: float) -> str:
    if pct >= 80:
        return _STATS_RED
    if pct >= 60:
        return _STATS_YELLOW
    return _STATS_GREEN


def _free_colour(free_pct: float) -> str:
    """Return the Rich colour name for a volume's free-space percentage.

    Thresholds are based on how much space is FREE (not used):
    - free < 10%  → red   (critically low)
    - free < 25%  → yellow (getting low)
    - else        → green  (plenty of space)
    """
    if free_pct < _VOLUME_RED_THRESHOLD:
        return _STATS_RED
    if free_pct < _VOLUME_YELLOW_THRESHOLD:
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


def render_status_bar(snapshot: SystemStatsSnapshot, *, volume_index: int = 0) -> Text:
    """Render the one-line system-stats bar with fixed-width numeric columns.

    CPU %, RAM %, and free-RAM are rendered in fixed-width slots so the
    '│ Disk' and '│ Net' separators never shift horizontally as values
    change digit-count (anti-jitter layout):

      - CPU/RAM % : right-aligned to 3 chars  (``":>3"``)
      - free RAM  : value token right-aligned to 6 chars before `` free``
                    (covers M range 0–1023 and G range 1.0–999.9)

    Disk/Net rates use ``_fmt_rate(...).ljust(7)`` and are unchanged.
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
    out.append(f" {int(snapshot.cpu_pct):>3}%")

    out.append(" │ ", style="dim")
    out.append("RAM ")
    _bar(snapshot.ram_pct)
    out.append(f" {int(snapshot.ram_pct):>3}%")

    free = snapshot.ram_free_bytes
    if free >= 1024**3:
        free_str = f"{free / 1024 ** 3:.1f}G"
    else:
        free_str = f"{free / 1024 ** 2:.0f}M"
    out.append(f"  {free_str:>6} free")

    out.append(" │ ", style="dim")
    out.append(
        f"Disk r:{_fmt_rate(snapshot.disk_read_bps)}"
        f" w:{_fmt_rate(snapshot.disk_write_bps)}"
    )
    out.append(" │ ", style="dim")
    out.append(
        f"Net ↓{_fmt_rate(snapshot.net_down_bps)}" f" ↑{_fmt_rate(snapshot.net_up_bps)}"
    )

    if snapshot.volumes:
        vol = snapshot.volumes[volume_index % len(snapshot.volumes)]
        out.append(" │ ", style="dim")
        out.append("↻ ")
        out.append(
            f"{vol.mountpoint} {int(vol.free_pct)}% free",
            style=_free_colour(vol.free_pct),
        )

    return out


# ---------------------------------------------------------------------------
# Monitor class — entry point for MonitorRegistry
# ---------------------------------------------------------------------------


class Monitor:
    """Bundled machine-stats monitor: wraps SystemStatsModel for the registry.

    ``tick(now)`` returns the ``render_status_bar`` line with fixed-width,
    stable (non-jittering) CPU/RAM/free columns.
    """

    def __init__(self) -> None:
        self._model = SystemStatsModel()

    def tick(self, now: float) -> Text:
        """Sample system stats and return the rendered status-bar line.

        ``volume_index = int(now / _VOLUME_ROTATE_SECONDS)`` advances the
        displayed mount every ``_VOLUME_ROTATE_SECONDS`` seconds of wall-clock
        time (the monotonic float passed by MonitorRegistry).
        """
        volume_index = int(now / _VOLUME_ROTATE_SECONDS)
        return render_status_bar(self._model.tick(now), volume_index=volume_index)
